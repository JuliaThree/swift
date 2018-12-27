# Copyright (c) 2013 OpenStack Foundation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.
# See the License for the specific language governing permissions and
# limitations under the License.


import eventlet.greenio
from six.moves import urllib

from swift.common import exceptions
from swift.common import http
from swift.common import swob
from swift.common import utils
from swift.common import request_helpers
from swift.common.utils import Timestamp


def decode_missing(line):
    """
    Parse a string of the form generated by
    :py:func:`~swift.obj.ssync_sender.encode_missing` and return a dict
    with keys ``object_hash``, ``ts_data``, ``ts_meta``, ``ts_ctype``.

    The encoder for this line is
    :py:func:`~swift.obj.ssync_sender.encode_missing`
    """
    result = {}
    parts = line.split()
    result['object_hash'] = urllib.parse.unquote(parts[0])
    t_data = urllib.parse.unquote(parts[1])
    result['ts_data'] = Timestamp(t_data)
    result['ts_meta'] = result['ts_ctype'] = result['ts_data']
    if len(parts) > 2:
        # allow for a comma separated list of k:v pairs to future-proof
        subparts = urllib.parse.unquote(parts[2]).split(',')
        for item in [subpart for subpart in subparts if ':' in subpart]:
            k, v = item.split(':')
            if k == 'm':
                result['ts_meta'] = Timestamp(t_data, delta=int(v, 16))
            elif k == 't':
                result['ts_ctype'] = Timestamp(t_data, delta=int(v, 16))
    return result


def encode_wanted(remote, local):
    """
    Compare a remote and local results and generate a wanted line.

    :param remote: a dict, with ts_data and ts_meta keys in the form
                   returned by :py:func:`decode_missing`
    :param local: a dict, possibly empty, with ts_data and ts_meta keys
                  in the form returned :py:meth:`Receiver._check_local`

    The decoder for this line is
    :py:func:`~swift.obj.ssync_sender.decode_wanted`
    """
    want = {}
    if 'ts_data' in local:
        # we have something, let's get just the right stuff
        if remote['ts_data'] > local['ts_data']:
            want['data'] = True
        if 'ts_meta' in local and remote['ts_meta'] > local['ts_meta']:
            want['meta'] = True
        if ('ts_ctype' in local and remote['ts_ctype'] > local['ts_ctype']
                and remote['ts_ctype'] > remote['ts_data']):
            want['meta'] = True
    else:
        # we got nothing, so we'll take whatever the remote has
        want['data'] = True
        want['meta'] = True
    if want:
        # this is the inverse of _decode_wanted's key_map
        key_map = dict(data='d', meta='m')
        parts = ''.join(v for k, v in sorted(key_map.items()) if want.get(k))
        return '%s %s' % (urllib.parse.quote(remote['object_hash']), parts)
    return None


class Receiver(object):
    """
    Handles incoming SSYNC requests to the object server.

    These requests come from the object-replicator daemon that uses
    :py:mod:`.ssync_sender`.

    The number of concurrent SSYNC requests is restricted by
    use of a replication_semaphore and can be configured with the
    object-server.conf [object-server] replication_concurrency
    setting.

    An SSYNC request is really just an HTTP conduit for
    sender/receiver replication communication. The overall
    SSYNC request should always succeed, but it will contain
    multiple requests within its request and response bodies. This
    "hack" is done so that replication concurrency can be managed.

    The general process inside an SSYNC request is:

        1. Initialize the request: Basic request validation, mount check,
           acquire semaphore lock, etc..

        2. Missing check: Sender sends the hashes and timestamps of
           the object information it can send, receiver sends back
           the hashes it wants (doesn't have or has an older
           timestamp).

        3. Updates: Sender sends the object information requested.

        4. Close down: Release semaphore lock, etc.
    """

    def __init__(self, app, request):
        self.app = app
        self.request = request
        self.device = None
        self.partition = None
        self.fp = None
        # We default to dropping the connection in case there is any exception
        # raised during processing because otherwise the sender could send for
        # quite some time before realizing it was all in vain.
        self.disconnect = True
        self.initialize_request()

    def __call__(self):
        """
        Processes an SSYNC request.

        Acquires a semaphore lock and then proceeds through the steps
        of the SSYNC process.
        """
        # The general theme for functions __call__ calls is that they should
        # raise exceptions.MessageTimeout for client timeouts (logged locally),
        # swob.HTTPException classes for exceptions to return to the caller but
        # not log locally (unmounted, for example), and any other Exceptions
        # will be logged with a full stack trace.
        #       This is because the client is never just some random user but
        # is instead also our code and we definitely want to know if our code
        # is broken or doing something unexpected.
        try:
            # Double try blocks in case our main error handlers fail.
            try:
                # Need to send something to trigger wsgi to return response
                # headers and kick off the ssync exchange.
                yield '\r\n'
                # If semaphore is in use, try to acquire it, non-blocking, and
                # return a 503 if it fails.
                if self.app.replication_semaphore:
                    if not self.app.replication_semaphore.acquire(False):
                        raise swob.HTTPServiceUnavailable()
                try:
                    with self.diskfile_mgr.replication_lock(self.device,
                                                            self.policy,
                                                            self.partition):
                        for data in self.missing_check():
                            yield data
                        for data in self.updates():
                            yield data
                    # We didn't raise an exception, so end the request
                    # normally.
                    self.disconnect = False
                finally:
                    if self.app.replication_semaphore:
                        self.app.replication_semaphore.release()
            except exceptions.ReplicationLockTimeout as err:
                self.app.logger.debug(
                    '%s/%s/%s SSYNC LOCK TIMEOUT: %s' % (
                        self.request.remote_addr, self.device, self.partition,
                        err))
                yield ':ERROR: %d %r\n' % (0, str(err))
            except exceptions.MessageTimeout as err:
                self.app.logger.error(
                    '%s/%s/%s TIMEOUT in ssync.Receiver: %s' % (
                        self.request.remote_addr, self.device, self.partition,
                        err))
                yield ':ERROR: %d %r\n' % (408, str(err))
            except swob.HTTPException as err:
                body = ''.join(err({}, lambda *args: None))
                yield ':ERROR: %d %r\n' % (err.status_int, body)
            except Exception as err:
                self.app.logger.exception(
                    '%s/%s/%s EXCEPTION in ssync.Receiver' %
                    (self.request.remote_addr, self.device, self.partition))
                yield ':ERROR: %d %r\n' % (0, str(err))
        except Exception:
            self.app.logger.exception('EXCEPTION in ssync.Receiver')
        if self.disconnect:
            # This makes the socket close early so the remote side doesn't have
            # to send its whole request while the lower Eventlet-level just
            # reads it and throws it away. Instead, the connection is dropped
            # and the remote side will get a broken-pipe exception.
            try:
                socket = self.request.environ['wsgi.input'].get_socket()
                eventlet.greenio.shutdown_safe(socket)
                socket.close()
            except Exception:
                pass  # We're okay with the above failing.

    def initialize_request(self):
        """
        Basic validation of request and mount check.

        This function will be called before attempting to acquire a
        replication semaphore lock, so contains only quick checks.
        """
        # This environ override has been supported since eventlet 0.14:
        # https://bitbucket.org/eventlet/eventlet/commits/ \
        #     4bd654205a4217970a57a7c4802fed7ff2c8b770
        self.request.environ['eventlet.minimum_write_chunk_size'] = 0
        self.device, self.partition, self.policy = \
            request_helpers.get_name_and_placement(self.request, 2, 2, False)

        self.frag_index = self.node_index = None
        if self.request.headers.get('X-Backend-Ssync-Frag-Index'):
            try:
                self.frag_index = int(
                    self.request.headers['X-Backend-Ssync-Frag-Index'])
            except ValueError:
                raise swob.HTTPBadRequest(
                    'Invalid X-Backend-Ssync-Frag-Index %r' %
                    self.request.headers['X-Backend-Ssync-Frag-Index'])
        if self.request.headers.get('X-Backend-Ssync-Node-Index'):
            try:
                self.node_index = int(
                    self.request.headers['X-Backend-Ssync-Node-Index'])
            except ValueError:
                raise swob.HTTPBadRequest(
                    'Invalid X-Backend-Ssync-Node-Index %r' %
                    self.request.headers['X-Backend-Ssync-Node-Index'])
            if self.node_index != self.frag_index:
                # a primary node should only receive it's own fragments
                raise swob.HTTPBadRequest(
                    'Frag-Index (%s) != Node-Index (%s)' % (
                        self.frag_index, self.node_index))
        utils.validate_device_partition(self.device, self.partition)
        self.diskfile_mgr = self.app._diskfile_router[self.policy]
        if not self.diskfile_mgr.get_dev_path(self.device):
            raise swob.HTTPInsufficientStorage(drive=self.device)
        self.fp = self.request.environ['wsgi.input']

    def _check_local(self, remote, make_durable=True):
        """
        Parse local diskfile and return results of current
        representative for comparison to remote.

        :param object_hash: the hash of the remote object being offered
        """
        try:
            df = self.diskfile_mgr.get_diskfile_from_hash(
                self.device, self.partition, remote['object_hash'],
                self.policy, frag_index=self.frag_index, open_expired=True)
        except exceptions.DiskFileNotExist:
            return {}
        try:
            df.open()
        except exceptions.DiskFileDeleted as err:
            result = {'ts_data': err.timestamp}
        except exceptions.DiskFileError:
            result = {}
        else:
            result = {
                'ts_data': df.data_timestamp,
                'ts_meta': df.timestamp,
                'ts_ctype': df.content_type_timestamp,
            }
        if (make_durable and df.fragments and
            remote['ts_data'] in df.fragments and
            self.frag_index in df.fragments[remote['ts_data']] and
            (df.durable_timestamp is None or
             df.durable_timestamp < remote['ts_data'])):
            # We have the frag, just missing durable state, so make the frag
            # durable now. Try this just once to avoid looping if it fails.
            try:
                with df.create() as writer:
                    writer.commit(remote['ts_data'])
                return self._check_local(remote, make_durable=False)
            except Exception:
                # if commit fails then log exception and fall back to wanting
                # a full update
                self.app.logger.exception(
                    '%s/%s/%s EXCEPTION in ssync.Receiver while '
                    'attempting commit of %s'
                    % (self.request.remote_addr, self.device, self.partition,
                       df._datadir))
        return result

    def _check_missing(self, line):
        """
        Parse offered object from sender, and compare to local diskfile,
        responding with proper protocol line to represented needed data
        or None if in sync.

        Anchor point for tests to mock legacy protocol changes.
        """
        remote = decode_missing(line)
        local = self._check_local(remote)
        return encode_wanted(remote, local)

    def missing_check(self):
        """
        Handles the receiver-side of the MISSING_CHECK step of a
        SSYNC request.

        Receives a list of hashes and timestamps of object
        information the sender can provide and responds with a list
        of hashes desired, either because they're missing or have an
        older timestamp locally.

        The process is generally:

            1. Sender sends `:MISSING_CHECK: START` and begins
               sending `hash timestamp` lines.

            2. Receiver gets `:MISSING_CHECK: START` and begins
               reading the `hash timestamp` lines, collecting the
               hashes of those it desires.

            3. Sender sends `:MISSING_CHECK: END`.

            4. Receiver gets `:MISSING_CHECK: END`, responds with
               `:MISSING_CHECK: START`, followed by the list of
               <wanted_hash> specifiers it collected as being wanted
               (one per line), `:MISSING_CHECK: END`, and flushes any
               buffers.

               Each <wanted_hash> specifier has the form <hash>[ <parts>] where
               <parts> is a string containing characters 'd' and/or 'm'
               indicating that only data or meta part of object respectively is
               required to be sync'd.

            5. Sender gets `:MISSING_CHECK: START` and reads the list
               of hashes desired by the receiver until reading
               `:MISSING_CHECK: END`.

        The collection and then response is so the sender doesn't
        have to read while it writes to ensure network buffers don't
        fill up and block everything.
        """
        with exceptions.MessageTimeout(
                self.app.client_timeout, 'missing_check start'):
            line = self.fp.readline(self.app.network_chunk_size)
        if line.strip() != ':MISSING_CHECK: START':
            raise Exception(
                'Looking for :MISSING_CHECK: START got %r' % line[:1024])
        object_hashes = []
        while True:
            with exceptions.MessageTimeout(
                    self.app.client_timeout, 'missing_check line'):
                line = self.fp.readline(self.app.network_chunk_size)
            if not line or line.strip() == ':MISSING_CHECK: END':
                break
            want = self._check_missing(line)
            if want:
                object_hashes.append(want)
        yield ':MISSING_CHECK: START\r\n'
        if object_hashes:
            yield '\r\n'.join(object_hashes)
        yield '\r\n'
        yield ':MISSING_CHECK: END\r\n'

    def updates(self):
        """
        Handles the UPDATES step of an SSYNC request.

        Receives a set of PUT and DELETE subrequests that will be
        routed to the object server itself for processing. These
        contain the information requested by the MISSING_CHECK step.

        The PUT and DELETE subrequests are formatted pretty much
        exactly like regular HTTP requests, excepting the HTTP
        version on the first request line.

        The process is generally:

            1. Sender sends `:UPDATES: START` and begins sending the
               PUT and DELETE subrequests.

            2. Receiver gets `:UPDATES: START` and begins routing the
               subrequests to the object server.

            3. Sender sends `:UPDATES: END`.

            4. Receiver gets `:UPDATES: END` and sends `:UPDATES:
               START` and `:UPDATES: END` (assuming no errors).

            5. Sender gets `:UPDATES: START` and `:UPDATES: END`.

        If too many subrequests fail, as configured by
        replication_failure_threshold and replication_failure_ratio,
        the receiver will hang up the request early so as to not
        waste any more time.

        At step 4, the receiver will send back an error if there were
        any failures (that didn't cause a hangup due to the above
        thresholds) so the sender knows the whole was not entirely a
        success. This is so the sender knows if it can remove an out
        of place partition, for example.
        """
        with exceptions.MessageTimeout(
                self.app.client_timeout, 'updates start'):
            line = self.fp.readline(self.app.network_chunk_size)
        if line.strip() != ':UPDATES: START':
            raise Exception('Looking for :UPDATES: START got %r' % line[:1024])
        successes = 0
        failures = 0
        while True:
            with exceptions.MessageTimeout(
                    self.app.client_timeout, 'updates line'):
                line = self.fp.readline(self.app.network_chunk_size)
            if not line or line.strip() == ':UPDATES: END':
                break
            # Read first line METHOD PATH of subrequest.
            method, path = line.strip().split(' ', 1)
            subreq = swob.Request.blank(
                '/%s/%s%s' % (self.device, self.partition, path),
                environ={'REQUEST_METHOD': method})
            # Read header lines.
            content_length = None
            replication_headers = []
            while True:
                with exceptions.MessageTimeout(self.app.client_timeout):
                    line = self.fp.readline(self.app.network_chunk_size)
                if not line:
                    raise Exception(
                        'Got no headers for %s %s' % (method, path))
                line = line.strip()
                if not line:
                    break
                header, value = line.split(':', 1)
                header = header.strip().lower()
                value = value.strip()
                subreq.headers[header] = value
                if header != 'etag':
                    # make sure ssync doesn't cause 'Etag' to be added to
                    # obj metadata in addition to 'ETag' which object server
                    # sets (note capitalization)
                    replication_headers.append(header)
                if header == 'content-length':
                    content_length = int(value)
            # Establish subrequest body, if needed.
            if method in ('DELETE', 'POST'):
                if content_length not in (None, 0):
                    raise Exception(
                        '%s subrequest with content-length %s'
                        % (method, path))
            elif method == 'PUT':
                if content_length is None:
                    raise Exception(
                        'No content-length sent for %s %s' % (method, path))

                def subreq_iter():
                    left = content_length
                    while left > 0:
                        with exceptions.MessageTimeout(
                                self.app.client_timeout,
                                'updates content'):
                            chunk = self.fp.read(
                                min(left, self.app.network_chunk_size))
                        if not chunk:
                            raise exceptions.ChunkReadError(
                                'Early termination for %s %s' % (method, path))
                        left -= len(chunk)
                        yield chunk
                subreq.environ['wsgi.input'] = utils.FileLikeIter(
                    subreq_iter())
            else:
                raise Exception('Invalid subrequest method %s' % method)
            subreq.headers['X-Backend-Storage-Policy-Index'] = int(self.policy)
            subreq.headers['X-Backend-Replication'] = 'True'
            if self.node_index is not None:
                # primary node should not 409 if it has a non-primary fragment
                subreq.headers['X-Backend-Ssync-Frag-Index'] = self.node_index
            if replication_headers:
                subreq.headers['X-Backend-Replication-Headers'] = \
                    ' '.join(replication_headers)
            # Route subrequest and translate response.
            resp = subreq.get_response(self.app)
            if http.is_success(resp.status_int) or \
                    resp.status_int == http.HTTP_NOT_FOUND:
                successes += 1
            else:
                self.app.logger.warning(
                    'ssync subrequest failed with %s: %s %s' %
                    (resp.status_int, method, subreq.path))
                failures += 1
            if failures >= self.app.replication_failure_threshold and (
                    not successes or
                    float(failures) / successes >
                    self.app.replication_failure_ratio):
                raise Exception(
                    'Too many %d failures to %d successes' %
                    (failures, successes))
            # The subreq may have failed, but we want to read the rest of the
            # body from the remote side so we can continue on with the next
            # subreq.
            for junk in subreq.environ['wsgi.input']:
                pass
        if failures:
            raise swob.HTTPInternalServerError(
                'ERROR: With :UPDATES: %d failures to %d successes' %
                (failures, successes))
        yield ':UPDATES: START\r\n'
        yield ':UPDATES: END\r\n'
