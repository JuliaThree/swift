# Copyright (c) 2014 OpenStack Foundation
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

"""
Object versioning in Swift has 3 different modes. There are two
:ref:`legacy modes <versioned_writes>` that have similar API with a slight
difference in behavior and this middleware introduces a new mode with a
completely redesigned API and implementation.

In terms of the implementation, this middleware relies heavily on the use of
static links to reduce the amount of backend data movement that was part of the
two legacy modes. It also introduces a new API for enabling the feature and to
interact with older versions of an object.

Compatibility between modes
===========================

This new mode is not backwards compatible or interchangeable with the
two legacy modes. This means that existing containers that are being versioned
by the two legacy modes cannot enable the new mode. The new mode can only be
enabled on a new container or a container without either
``X-Versions-Location`` or ``X-History-Location`` header set. Attempting to
enable the new mode on a container with either header will result in a
``400 Bad Request`` response.

Enable Object Versioning in a Container
=======================================

After the introduction of this feature containers in a Swift cluster will be
in one of either 3 possible states: 1. Object versioning never enabled,
2. Object Versioning Enabled or 3. Object Versioning Disabled. Once versioning
has been enabled on a container, it will always have a flag stating whether it
is either enabled or disabled.

Clients enable object versioning on a container by performing either a PUT or
POST request with the header ``X-Versions-Enabled: true``. Upon enabling the
versioning for the first time, the middleware will create a hidden container
where object versions are stored. This hidden container will inherit the same
Storage Policy as its parent container.

To disable, clients send a POST request with the header
``X-Versions-Enabled: false``. When versioning is disabled, the old versions
remain unchanged.

To delete a versioned container, versioning must be disabled and all versions
of all objects must be deleted before the container can be deleted. At such
time, the hidden container will also be deleted.

Object CRUD Operations to a Versioned Container
===============================================

When data is ``PUT`` into a versioned container (a container with the
versioning flag enabled), the actual object is written to a hidden container
and a symlink object is written to the parent container. Every object is
assigned a version id. This id can be retrieved from the
``X-Object-Version-Id`` header in the PUT response.

.. note::

    When object versioning is disabled on a container, new data will no longer
    be versioned, but older versions remain untouched. Any new data ``PUT``
    will result in a object with a ``null`` version-id. The versioning API can
    be used to both list and operate on previous versions even while versioning
    is disabled.

    If versioning is re-enabled and an overwrite occurs on a `null` id object.
    The object will be versioned off with a regular version-id.

A ``GET`` to a versioned object will return the current version of the object.
The ``X-Object-Version-Id`` header is also returned in the response.

A ``POST`` to a versioned object will update the most current object metadata
as normal, but will not create a new version of the object. In other words,
new versions are only created when the content of the object changes.

On ``DELETE``, the middleware will write a zero-byte "delete marker" object
version that notes **when** the delete took place. The symlink object will also
be deleted from the versioned container. The object will no longer appear in
container listings for the versioned container and future requests there will
return ``404 Not Found``. However, the previous versions content will still be
recoverable.

Object Versioning API
=====================

Clients can now operate on previous versions of an object using this new
versioning API.

First to list previous versions, issue a a ``GET`` request to the versioned
container with query parameter::

    ?versions

To list a container with a large number of object versions, clients can
also use the ``version_marker`` parameter together with the ``marker``
parameter.  While the ``marker`` parameter is used to specify an object name
the ``version_marker`` will be used specify the version id.

All other pagination parameters can be used in conjunction with the
``versions`` parameter.

During container listings, delete markers can be identified with the
content-type ``application/x-deleted;swift_versions_deleted=1``. The most
current version of an object can be identified by the field ``is_latest``.

To operate on previous versions, clients can use the query parameter::

    ?version-id=<id>

where the ``<id>`` is the value from the ``X-Object-Version-Id`` header.

Only COPY, HEAD, GET and DELETE operations can be performed on previous
versions. Either a PUT or POST request with a ``version-id`` parameter will
result in a ``400 Bad Request`` response.

A HEAD/GET request to a delete-marker will result in a ``404 Not Found``
response.

When issuing DELETE requests with a ``version-id`` parameter, delete markers
are not written down. A DELETE request with a ``version-id`` parameter to
the current object will result in a both the symlink and the backing data
being deleted. A DELETE to any other version will result in that version only
be deleted and no changes made to the symlink pointing to the current version.

How to Enable Object Versioning in a Swift Cluster
==================================================

To enable this new mode in a Swift cluster the ``versioned_writes`` and
``symlink`` middlewares must be added to the proxy pipeline, you must also set
the option ``allow_object_versioning`` to ``True``.
"""

import calendar
import itertools
import json
import six
import time

from cgi import parse_header
from six.moves.urllib.parse import unquote

from swift.common.constraints import MAX_FILE_SIZE
from swift.common.exceptions import ListingIterNotFound, ListingIterError
from swift.common.http import is_success, is_client_error, HTTP_NOT_FOUND
from swift.common.request_helpers import get_sys_meta_prefix, \
    copy_header_subset, get_reserved_name, split_reserved_name
from swift.common.middleware.symlink import TGT_OBJ_SYMLINK_HDR, \
    TGT_ETAG_SYSMETA_SYMLINK_HDR, SYMLOOP_EXTEND, ALLOW_RESERVED_NAMES, \
    TGT_BYTES_SYSMETA_SYMLINK_HDR, TGT_ACCT_SYMLINK_HDR
from swift.common.swob import HTTPPreconditionFailed, HTTPServiceUnavailable, \
    HTTPServerError, HTTPBadRequest, str_to_wsgi, bytes_to_wsgi, wsgi_quote, \
    wsgi_to_str, wsgi_unquote, Request, HTTPNotFound, HTTPException, \
    HTTPRequestEntityTooLarge, HTTPInternalServerError, HTTPNotAcceptable
from swift.common.storage_policy import POLICIES
from swift.common.utils import get_logger, Timestamp, \
    config_true_value, close_if_possible, closing_if_possible, \
    FileLikeIter, split_path, parse_content_type, RESERVED_STR
from swift.common.wsgi import WSGIContext, make_pre_authed_request
from swift.proxy.controllers.base import get_container_info


DELETE_MARKER_CONTENT_TYPE = 'application/x-deleted;swift_versions_deleted=1'
CLIENT_VERSIONS_ENABLED = 'x-versions-enabled'
SYSMETA_VERSIONS_ENABLED = \
    get_sys_meta_prefix('container') + 'versions-enabled'
SYSMETA_VERSIONS_CONT = get_sys_meta_prefix('container') + 'versions-container'
SYSMETA_PARENT_CONT = get_sys_meta_prefix('container') + 'parent-container'
SYSMETA_VERSIONS_SYMLINK = get_sys_meta_prefix('object') + 'versions-symlink'


def build_listing(*to_splice, **kwargs):
    reverse = kwargs.pop('reverse')
    if kwargs:
        raise TypeError('Invalid keyword arguments received: %r' % kwargs)

    def merge_key(item):
        if 'subdir' in item:
            return item['subdir']
        return item['name']

    return json.dumps(sorted(
        itertools.chain(*to_splice),
        key=merge_key,
        reverse=reverse,
    )).encode('ascii')


class ByteCountingReader(object):
    """
    Counts bytes read from file_like so we know how big the object is that
    the client just PUT.

    This is particularly important when the client sends a chunk-encoded body,
    so we don't have a Content-Length header available.
    """
    def __init__(self, file_like):
        self.file_like = file_like
        self.bytes_read = 0

    def read(self, amt=-1):
        chunk = self.file_like.read(amt)
        self.bytes_read += len(chunk)
        return chunk


class ObjectVersioningContext(WSGIContext):
    def __init__(self, wsgi_app, logger):
        super(ObjectVersioningContext, self).__init__(wsgi_app)
        self.logger = logger

    def _build_versions_object_prefix(self, object_name):
        return get_reserved_name(object_name, '')

    def _build_versions_container_name(self, container_name):
        return get_reserved_name('versions', container_name)

    def _build_versions_object_name(self, object_name, ts):
        inv = ~Timestamp(ts)
        return get_reserved_name(object_name, inv.internal)

    def _split_version_from_name(self, versioned_name):
        try:
            name, inv = split_reserved_name(versioned_name)
            ts = ~Timestamp(inv)
        except ValueError:
            return versioned_name, None
        return name, ts


class ObjectContext(ObjectVersioningContext):
    def _listing_iter(self, account_name, lcontainer, lprefix, req):
        try:
            for page in self._listing_pages_iter(account_name, lcontainer,
                                                 lprefix, req):
                for item in page:
                    yield item
        except ListingIterNotFound:
            pass
        except ListingIterError:
            raise HTTPServerError(request=req)

    def _listing_pages_iter(self, account_name, lcontainer, lprefix,
                            req, marker='', end_marker=''):
        '''Get "pages" worth of objects that start with a prefix.

        The optional keyword arguments ``marker`` and ``end_marker``
        are used similar to how they are for containers. We're
        coming directly from ``_listing_iter``, so none of the
        optional args are specified.
        '''
        while True:
            lreq = make_pre_authed_request(
                req.environ, method='GET', swift_source='VW',
                headers={'X-Backend-Allow-Reserved-Names': 'true'},
                path=wsgi_quote('/v1/%s/%s' % (account_name, lcontainer)))
            lreq.environ['QUERY_STRING'] = \
                'prefix=%s&marker=%s' % (wsgi_quote(lprefix),
                                         wsgi_quote(marker))
            if end_marker:
                lreq.environ['QUERY_STRING'] += '&end_marker=%s' % (
                    wsgi_quote(end_marker))
            lresp = lreq.get_response(self.app)
            if not is_success(lresp.status_int):
                close_if_possible(lresp.app_iter)
                if lresp.status_int == HTTP_NOT_FOUND:
                    raise ListingIterNotFound()
                elif is_client_error(lresp.status_int):
                    raise HTTPPreconditionFailed(request=req)
                else:
                    raise ListingIterError()

            if not lresp.body:
                break
            sublisting = json.loads(lresp.body)
            if not sublisting:
                break
            marker = bytes_to_wsgi(sublisting[-1]['name'].encode('utf-8'))

            sublisting = [
                item for item in sublisting
                for name in [bytes_to_wsgi(item['name'].encode('utf-8'))]
                if self._split_version_from_name(name)[1] is not None]

            if not sublisting:
                continue
            yield sublisting

    def _get_source_object(self, req, path_info):
        # make a pre_auth request in case the user has write access
        # to container, but not READ. This was allowed in previous version
        # (i.e., before middleware) so keeping the same behavior here
        get_req = make_pre_authed_request(
            req.environ, path=wsgi_quote(path_info) + '?symlink=get',
            headers={'X-Newest': 'True'}, method='GET', swift_source='VW')
        source_resp = get_req.get_response(self.app)

        if source_resp.content_length is None or \
                source_resp.content_length > MAX_FILE_SIZE:
            close_if_possible(source_resp.app_iter)
            return HTTPRequestEntityTooLarge(request=req)

        return source_resp

    def _put_versioned_obj(self, req, put_path_info, source_resp):
        # Create a new Request object to PUT to the versions container, copying
        # all headers from the source object apart from x-timestamp.
        put_req = make_pre_authed_request(
            req.environ, path=wsgi_quote(put_path_info), method='PUT',
            headers={'X-Backend-Allow-Reserved-Names': 'true'},
            swift_source='VW')
        copy_header_subset(source_resp, put_req,
                           lambda k: k.lower() != 'x-timestamp')
        put_req.environ['wsgi.input'] = FileLikeIter(source_resp.app_iter)
        slo_size = put_req.headers.get('X-Object-Sysmeta-Slo-Size')
        if slo_size:
            put_req.headers['Content-Type'] += '; swift_bytes=%s' % slo_size
            put_req.environ['swift.content_type_overridden'] = True
        put_resp = put_req.get_response(self.app)
        close_if_possible(source_resp.app_iter)
        return put_resp

    def _put_versioned_obj_from_client(self, req, versions_cont, api_version,
                                       account_name, object_name):
        vers_obj_name = self._build_versions_object_name(
            object_name, req.timestamp.internal)
        put_path_info = "/%s/%s/%s/%s" % (
            api_version, account_name, versions_cont, vers_obj_name)
        # Consciously *do not* set swift_source here -- this req is in charge
        # of reading bytes from the client, don't let it look like that data
        # movement is due to some internal-to-swift thing
        put_req = make_pre_authed_request(
            req.environ, path=wsgi_quote(put_path_info), method='PUT',
            headers={'X-Backend-Allow-Reserved-Names': 'true'})
        # move the client request body over
        # note that the WSGI environ may be *further* manipulated; hold on to
        # a reference to the byte counter so we can get the bytes_read
        if req.message_length() is None:
            put_req.headers['transfer-encoding'] = \
                req.headers.get('transfer-encoding')
        else:
            put_req.content_length = req.content_length
        byte_counter = ByteCountingReader(req.environ['wsgi.input'])
        put_req.environ['wsgi.input'] = byte_counter
        req.body = b''
        # move metadata over, including sysmeta

        copy_header_subset(req, put_req, lambda k: True)
        if 'swift.content_type_overridden' in req.environ:
            put_req.environ['swift.content_type_overridden'] = \
                req.environ.pop('swift.content_type_overridden')

        # do the write
        put_resp = put_req.get_response(self.app)
        self._check_response_error(req, put_resp)
        with closing_if_possible(put_resp.app_iter), closing_if_possible(
                put_req.environ['wsgi.input']):
            for chunk in put_resp.app_iter:
                pass
        put_bytes = byte_counter.bytes_read
        # N.B. this is essentially the same hack that symlink does in
        # _validate_etag_and_update_sysmeta to deal with SLO
        slo_size = put_req.headers.get('X-Object-Sysmeta-Slo-Size')
        if slo_size:
            put_bytes = slo_size
        put_content_type = parse_content_type(
            put_req.headers['Content-Type'])[0]

        return (put_resp, vers_obj_name, put_bytes, put_content_type)

    def _put_symlink_to_version(self, req, versions_cont, put_vers_obj_name,
                                api_version, account_name, object_name,
                                put_etag, put_bytes, put_content_type):

        req.method = 'PUT'
        # inch x-timestamp forward, just in case
        req.ensure_x_timestamp()
        req.headers['X-Timestamp'] = Timestamp(
            req.timestamp, offset=1).internal
        req.headers[TGT_ETAG_SYSMETA_SYMLINK_HDR] = put_etag
        req.headers[TGT_BYTES_SYSMETA_SYMLINK_HDR] = put_bytes
        # N.B. in stack mode DELETE we use content_type from listing
        req.headers['Content-Type'] = put_content_type
        req.headers[TGT_OBJ_SYMLINK_HDR] = wsgi_quote('%s/%s' % (
            versions_cont, put_vers_obj_name))
        req.headers[SYSMETA_VERSIONS_SYMLINK] = 'true'
        req.headers[SYMLOOP_EXTEND] = 'true'
        req.headers[ALLOW_RESERVED_NAMES] = 'true'
        req.headers['X-Backend-Allow-Reserved-Names'] = 'true'
        not_for_symlink_headers = (
            'ETag', 'X-If-Delete-At', TGT_ACCT_SYMLINK_HDR,
            'X-Object-Manifest', 'X-Static-Large-Object',
            'X-Object-Sysmeta-Slo-Etag', 'X-Object-Sysmeta-Slo-Size',
        )
        for header in not_for_symlink_headers:
            req.headers.pop(header, None)

        # *do* set swift_source here; this PUT is an implementation detail
        req.environ['swift.source'] = 'VW'
        req.body = b''
        resp = req.get_response(self.app)
        resp.headers['ETag'] = put_etag
        resp.headers['X-Object-Version-Id'] = self._split_version_from_name(
            put_vers_obj_name)[1].internal
        return resp

    def _check_response_error(self, req, resp):
        """
        Raise Error Response in case of error
        """
        if is_success(resp.status_int):
            return
        close_if_possible(resp.app_iter)
        if is_client_error(resp.status_int):
            # missing container or bad permissions
            if resp.status_int == 404:
                raise HTTPPreconditionFailed(request=req)
            raise HTTPException(body=resp.body, status=resp.status,
                                headers=resp.headers)
        # could not version the data, bail
        raise HTTPServiceUnavailable(request=req)

    def _copy_current(self, req, versions_cont, api_version, account_name,
                      object_name):
        '''
        Check if the current version of the object is a versions-symlink
        if not, it's because this object was added to the container when
        versioning was not enabled. We'll need to copy it into the versions
        containers now.

        :param req: original request.
        :param versions_cont: container where previous versions of the object
                              are stored.
        :param api_version: api version.
        :param account_name: account name.
        :param object_name: name of object of original request
        '''
        # validate the write access to the versioned container before
        # making any backend requests
        if 'swift.authorize' in req.environ:
            container_info = get_container_info(
                req.environ, self.app)
            req.acl = container_info.get('write_acl')
            aresp = req.environ['swift.authorize'](req)
            if aresp:
                raise aresp

        get_resp = self._get_source_object(req, req.path_info)

        if get_resp.status_int == HTTP_NOT_FOUND:
            # nothing to version, proceed with original request
            close_if_possible(get_resp.app_iter)
            return get_resp

        # check for any other errors
        self._check_response_error(req, get_resp)

        if get_resp.headers.get(SYSMETA_VERSIONS_SYMLINK) == 'true':
            # existing object is a VW symlink; no action required
            close_if_possible(get_resp.app_iter)
            return get_resp

        # if there's an existing object, then copy it to
        # X-Versions-Location
        ts_source = get_resp.headers.get(
            'x-timestamp',
            calendar.timegm(time.strptime(
                get_resp.headers['last-modified'],
                '%a, %d %b %Y %H:%M:%S GMT')))
        vers_obj_name = self._build_versions_object_name(
            object_name, ts_source)

        put_path_info = "/%s/%s/%s/%s" % (
            api_version, account_name, versions_cont, vers_obj_name)
        put_resp = self._put_versioned_obj(req, put_path_info, get_resp)

        self._check_response_error(req, put_resp)
        close_if_possible(put_resp.app_iter)
        return put_resp

    def handle_put(self, req, versions_cont, api_version,
                   account_name, object_name, is_enabled):
        """
        Check if the current version of the object is a versions-symlink
        if not, it's because this object was added to the container when
        versioning was not enabled. We'll need to copy it into the versions
        containers now that versioning is enabled.

        Also, put the new data from the client into the versions container
        and add a static symlink in the versioned container.

        :param req: original request.
        :param versions_cont: container where previous versions of the object
                              are stored.
        :param api_version: api version.
        :param account_name: account name.
        :param object_name: name of object of original request
        """
        # handle object request for a disabled versioned container.
        if not is_enabled:
            return req.get_response(self.app)

        # attempt to copy current object to versions container
        self._copy_current(req, versions_cont, api_version, account_name,
                           object_name)

        # write client's put directly to versioned container
        req.ensure_x_timestamp()
        put_resp, put_vers_obj_name, put_bytes, put_content_type = \
            self._put_versioned_obj_from_client(req, versions_cont,
                                                api_version, account_name,
                                                object_name)

        # and add an static symlink to original container
        target_etag = put_resp.headers['Etag']
        return self._put_symlink_to_version(req, versions_cont,
                                            put_vers_obj_name, api_version,
                                            account_name, object_name,
                                            target_etag, put_bytes,
                                            put_content_type)

    def handle_delete(self, req, versions_cont, api_version,
                      account_name, container_name,
                      object_name, is_enabled):
        """
        Handle DELETE requests.

        Copy current version of object to versions_container and write a
        delete marker before proceeding with original request.

        :param req: original request.
        :param versions_cont: container where previous versions of the object
                              are stored.
        :param api_version: api version.
        :param account_name: account name.
        :param object_name: name of object of original request
        """
        # handle object request for a disabled versioned container.
        if not is_enabled:
            return req.get_response(self.app)

        self._copy_current(req, versions_cont, api_version,
                           account_name, object_name)

        req.ensure_x_timestamp()
        marker_name = self._build_versions_object_name(
            object_name, req.timestamp.internal)
        marker_path = "/%s/%s/%s/%s" % (
            api_version, account_name, versions_cont, marker_name)
        marker_headers = {
            # Definitive source of truth is Content-Type, and since we add
            # a swift_* param, we know users haven't set it themselves.
            # This is still open to users POSTing to update the content-type
            # but they're just shooting themselves in the foot then.
            'content-type': DELETE_MARKER_CONTENT_TYPE,
            'content-length': '0',
            'x-auth-token': req.headers.get('x-auth-token'),
            'X-Backend-Allow-Reserved-Names': 'true',
        }
        marker_req = make_pre_authed_request(
            req.environ, path=wsgi_quote(marker_path),
            headers=marker_headers, method='PUT', swift_source='VW')
        marker_req.environ['swift.content_type_overridden'] = True
        marker_resp = marker_req.get_response(self.app)
        self._check_response_error(req, marker_resp)
        close_if_possible(marker_resp.app_iter)

        # successfully copied and created delete marker; safe to delete
        resp = req.get_response(self.app)
        if resp.is_success or resp.status_int == 404:
            resp.headers['X-Object-Version-Id'] = \
                self._split_version_from_name(marker_name)[1].internal
        close_if_possible(resp.app_iter)
        return resp

    def handle_post(self, req, versions_cont, account):
        '''
        Handle a POST request to an object in a versioned container.

        If the response is a 307 because the POST went to a symlink,
        follow the symlink and send the request to the versioned object

        :param req: original request.
        :param versions_cont: container where previous versions of the object
                              are stored.
        :param account: account name.
        '''
        # create eventual post request before
        # encryption middleware changes the request headers
        post_req = make_pre_authed_request(
            req.environ, path=wsgi_quote(req.path_info), method='POST',
            headers={'X-Backend-Allow-Reserved-Names': 'true'},
            swift_source='VW')
        copy_header_subset(req, post_req, lambda k: True)

        # send original request
        resp = req.get_response(self.app)

        # if it's a versioning symlink, send post to versioned object
        if resp.status_int == 307 and config_true_value(
                resp.headers.get(SYSMETA_VERSIONS_SYMLINK, 'false')):
            loc = wsgi_unquote(resp.headers['Location'])

            # Only follow if the version container matches
            if split_path(loc, 4, 4, True)[1:3] == [
                    account, versions_cont]:
                close_if_possible(resp.app_iter)
                post_req.path_info = loc
                resp = post_req.get_response(self.app)
        return resp

    def _check_head(self, req, auth_token_header):
        obj_head_headers = {
            'X-Newest': 'True',
            'X-Backend-Allow-Reserved-Names': 'true',
        }
        obj_head_headers.update(auth_token_header)
        head_req = make_pre_authed_request(
            req.environ, path=wsgi_quote(req.path_info) + '?symlink=get',
            method='HEAD', headers=obj_head_headers, swift_source='VW')
        hresp = head_req.get_response(self.app)
        head_is_tombstone = False
        symlink_target = None
        if hresp.status_int == HTTP_NOT_FOUND:
            head_is_tombstone = True
        else:
            head_is_tombstone = False
            # if there's any other kind of error with a broken link...
            # I guess give up?
            self._check_response_error(req, hresp)
            if hresp.headers.get(SYSMETA_VERSIONS_SYMLINK) == 'true':
                symlink_target = hresp.headers.get(TGT_OBJ_SYMLINK_HDR)
        close_if_possible(hresp.app_iter)
        return head_is_tombstone, symlink_target

    def handle_delete_version(self, req, versions_cont, api_version,
                              account_name, container_name,
                              object_name, is_enabled, version):
        if version == 'null':
            # let the request go directly through to the is_latest link
            return
        auth_token_header = {'X-Auth-Token': req.headers.get('X-Auth-Token')}
        head_is_tombstone, symlink_target = self._check_head(
            req, auth_token_header)

        versions_obj = self._build_versions_object_name(
            object_name, version)

        if (head_is_tombstone or (
            symlink_target and wsgi_unquote(symlink_target) != wsgi_unquote(
                '%s/%s' % (versions_cont, versions_obj)))):
            # if current version links to another version, then
            # just delete the version issue requested to delete
            req.path_info = "/%s/%s/%s/%s" % (
                api_version, account_name, versions_cont, versions_obj)
            req.headers['X-Backend-Allow-Reserved-Names'] = 'true'
            if head_is_tombstone:
                resp_version_id = 'null'
            else:
                _, vers_obj_name = wsgi_unquote(symlink_target).split('/', 1)
                resp_version_id = self._split_version_from_name(
                    vers_obj_name)[1].internal
        else:
            # if version-id is the latest version, delete the link too
            # First, kill the link...
            req.environ['QUERY_STRING'] = ''
            link_resp = req.get_response(self.app)
            self._check_response_error(req, link_resp)
            close_if_possible(link_resp.app_iter)

            # *then* the backing data
            req.path_info = "/%s/%s/%s/%s" % (
                api_version, account_name, versions_cont, versions_obj)
            req.headers['X-Backend-Allow-Reserved-Names'] = 'true'
            resp_version_id = 'null'
        resp = req.get_response(self.app)
        resp.headers['X-Object-Current-Version-Id'] = resp_version_id
        return resp

    def handle_put_version(self, req, versions_cont, api_version, account_name,
                           container, object_name, is_enabled, version):
        """
        Handle a PUT?version-id request and create/update the is_latest link to
        point to the specific version.
        """
        if req.content_length is None:
            has_body = (req.body_file.read(1) != b'')
        else:
            has_body = (req.content_length != 0)
        if has_body:
            raise HTTPBadRequest(
                body='PUT version-id requests require a zero byte body',
                request=req,
                content_type='text/plain')
        try:
            versions_obj_name = self._build_versions_object_name(
                object_name, version)
        except ValueError:
            raise HTTPBadRequest('Invalid version parameter')
        versioned_obj_path = "/%s/%s/%s/%s" % (
            api_version, account_name, versions_cont, versions_obj_name)
        obj_head_headers = {'X-Backend-Allow-Reserved-Names': 'true'}
        head_req = make_pre_authed_request(
            req.environ, path=wsgi_quote(versioned_obj_path) + '?symlink=get',
            method='HEAD', headers=obj_head_headers, swift_source='VW')
        head_resp = head_req.get_response(self.app)
        self._check_response_error(req, head_resp)
        close_if_possible(head_resp.app_iter)

        put_etag = head_resp.headers['ETag']
        put_bytes = head_resp.content_length
        put_content_type = head_resp.headers['Content-Type']
        resp = self._put_symlink_to_version(
            req, versions_cont, versions_obj_name, api_version, account_name,
            object_name, put_etag, put_bytes, put_content_type)
        return resp

    def handle_versioned_request(self, req, versions_cont, api_version,
                                 account, container, obj, is_enabled, version):
        """
        Handle 'version-id' request for object resource. When a request
        contains a ``version-id=<id>`` parameter, the request is acted upon
        the actual version of that object. Version-aware operations
        require that the container is versioned, but do not require that
        the versioning is currently enabled. Users should be able to
        operate on older versions of an object even if versioning is
        currently suspended.

        PUT and POST requests are not allowed as that would overwrite
        the contents of the versioned object.

        :param req: The original request
        :param versions_cont: container holding versions of the requested obj
        :param api_version: should be v1 unless swift bumps api version
        :param account: account name string
        :param container: container name string
        :param object: object name string
        :param is_enabled: is versioning currently enabled
        :param version: version of the object to act on
        """
        # ?version-id requests are allowed for GET, HEAD, DELETE reqs
        if req.method == 'POST':
            raise HTTPBadRequest(
                '%s to a specific version is not allowed' % req.method,
                request=req)
        elif not versions_cont and version != 'null':
            raise HTTPBadRequest(
                'version-aware operations require that the container is '
                'versioned', request=req)
        if version != 'null':
            try:
                Timestamp(version)
            except ValueError:
                raise HTTPBadRequest('Invalid version parameter')

        if req.method == 'DELETE':
            return self.handle_delete_version(
                req, versions_cont, api_version, account,
                container, obj, is_enabled, version)
        elif req.method == 'PUT':
            return self.handle_put_version(
                req, versions_cont, api_version, account,
                container, obj, is_enabled, version)
        if version == 'null':
            head_is_tombstone, symlink_target = self._check_head(
                req, {'X-Auth-Token': req.headers.get('X-Auth-Token')})
            if head_is_tombstone or symlink_target:
                raise HTTPNotFound(request=req)

            # else, primary container has the null version; let it through
            resp = req.get_response(self.app)
            if resp.is_success:
                resp.headers['X-Object-Version-Id'] = 'null'
                if req.method == 'HEAD':
                    close_if_possible(resp.app_iter)
            return resp
        else:
            # Re-write the path; most everything else goes through normally
            req.path_info = "/%s/%s/%s/%s" % (
                api_version, account, versions_cont,
                self._build_versions_object_name(obj, version))
            req.headers['X-Backend-Allow-Reserved-Names'] = 'true'

            resp = req.get_response(self.app)
            if resp.is_success:
                resp.headers['X-Object-Version-Id'] = version

            # Well, except for some delete marker business...
            is_del_marker = DELETE_MARKER_CONTENT_TYPE == resp.headers.get(
                'X-Backend-Content-Type', resp.headers['Content-Type'])

            if req.method == 'HEAD':
                close_if_possible(resp.app_iter)

            if is_del_marker:
                hdrs = {'X-Object-Version-Id': version,
                        'Content-Type': DELETE_MARKER_CONTENT_TYPE}
                raise HTTPNotFound(request=req, headers=hdrs)
            return resp

    def handle_request(self, req, versions_cont, api_version, account,
                       container, obj, is_enabled):
        if req.method == 'PUT':
            return self.handle_put(
                req, versions_cont, api_version, account, obj,
                is_enabled)
        elif req.method == 'POST':
            return self.handle_post(req, versions_cont, account)
        elif req.method == 'DELETE':
            return self.handle_delete(
                req, versions_cont, api_version, account,
                container, obj, is_enabled)

        # GET/HEAD/OPTIONS
        resp = req.get_response(self.app)

        resp.headers['X-Object-Version-Id'] = 'null'
        # Check for a "real" version
        loc = wsgi_unquote(resp.headers.get('Content-Location', ''))
        if loc:
            _, acct, cont, version_obj = split_path(loc, 4, 4, True)
            if acct == account and cont == versions_cont:
                _, version = self._split_version_from_name(version_obj)
                if version is not None:
                    resp.headers['X-Object-Version-Id'] = version.internal
                    content_loc = wsgi_quote('/%s/%s/%s/%s' % (
                        api_version, account, container, obj,
                    )) + '?version-id=%s' % (version.internal,)
                    resp.headers['Content-Location'] = content_loc
        return resp


class ContainerContext(ObjectVersioningContext):
    def handle_request(self, req, start_response):
        """
        Handle request for container resource.

        On PUT, POST set version location and enabled flag sysmeta.
        For container listings of a versioned container, update the object's
        bytes and etag to use the target's instead of using the symlink info.
        """
        app_resp = self._app_call(req.environ)
        container = req.split_path(3, 3, True)[2]
        if self._response_headers is None:
            self._response_headers = []
        location = ''
        curr_bytes = 0
        bytes_idx = -1
        for i, (header, value) in enumerate(self._response_headers):
            if header == 'X-Container-Bytes-Used':
                curr_bytes = value
                bytes_idx = i
            if header.lower() == SYSMETA_VERSIONS_CONT:
                location = value
            if header.lower() == SYSMETA_VERSIONS_ENABLED:
                self._response_headers.extend([
                    (CLIENT_VERSIONS_ENABLED.title(), value)])

        if location:
            location = wsgi_unquote(location)

            # update bytes header
            if bytes_idx > -1:
                account = req.split_path(3, 3, True)[1]
                head_req = make_pre_authed_request(
                    req.environ, method='HEAD', swift_source='VW',
                    path=wsgi_quote('/v1/%s/%s' % (account, location)),
                    headers={'X-Backend-Allow-Reserved-Names': 'true'})
                vresp = head_req.get_response(self.app)
                if vresp.is_success:
                    ver_bytes = vresp.headers.get('X-Container-Bytes-Used', 0)
                    self._response_headers[bytes_idx] = (
                        'X-Container-Bytes-Used',
                        str(int(curr_bytes) + int(ver_bytes)))
                close_if_possible(vresp.app_iter)
        elif is_success(self._get_status_int()):
            # If client is doing a version-aware listing for a container that
            # (as best we could tell) has never had versioning enabled,
            # err on the side of there being data anyway -- the metadata we
            # found may not be the most up-to-date.

            # Note that any extra listing request we make will likely 404.
            location = self._build_versions_container_name(container)
        # else, we won't need location anyway

        if is_success(self._get_status_int()) and req.method == 'GET':
            with closing_if_possible(app_resp):
                body = b''.join(app_resp)
            try:
                listing = json.loads(body)
            except ValueError:
                app_resp = [body]
            else:
                for item in listing:
                    if not all(x in item for x in (
                            'symlink_path',
                            'symlink_etag',
                            'symlink_bytes')):
                        continue
                    path = wsgi_unquote(bytes_to_wsgi(
                        item['symlink_path'].encode('utf-8')))
                    _, tgt_acct, tgt_container, tgt_obj = split_path(
                        path, 4, 4, True)
                    if tgt_container != location:
                        # if the archive container changed, leave the extra
                        # info unmolested
                        continue
                    _, meta = parse_header(item['hash'])
                    tgt_bytes = int(item.pop('symlink_bytes'))
                    item['bytes'] = tgt_bytes
                    item['version_symlink'] = True
                    item['hash'] = item.pop('symlink_etag') + ''.join(
                        '; %s=%s' % (k, v) for k, v in meta.items())
                    tgt_obj, version = self._split_version_from_name(tgt_obj)
                    if version is not None and 'versions' not in req.params:
                        sp = wsgi_quote('/v1/%s/%s/%s' % (
                            tgt_acct, container, tgt_obj,
                        )) + '?version-id=' + version.internal
                        item['symlink_path'] = sp

                if 'versions' in req.params:
                    return self._list_versions(
                        req, start_response, location,
                        listing)

                body = json.dumps(listing).encode('ascii')
                self.update_content_length(len(body))
                app_resp = [body]

        start_response(self._response_status,
                       self._response_headers,
                       self._response_exc_info)
        return app_resp

    def handle_delete(self, req, start_response):
        """
        Handle request to delete a user's container.

        As part of deleting a container, this middleware will also delete
        the hidden container holding object versions.

        Before a user's container can be deleted, swift must check
        if there are still old object versions from that container.
        Only after disabling versioning and deleting *all* object versions
        can a container be deleted.
        """
        container_info = get_container_info(req.environ, self.app)

        versions_cont = unquote(container_info.get(
            'sysmeta', {}).get('versions-container', ''))

        if versions_cont:
            account = req.split_path(3, 3, True)[1]
            versions_req = make_pre_authed_request(
                req.environ, method='HEAD', swift_source='VW',
                path=wsgi_quote('/v1/%s/%s' % (
                    account, str_to_wsgi(versions_cont))),
                headers={'X-Backend-Allow-Reserved-Names': 'true'})
            vresp = versions_req.get_response(self.app)
            close_if_possible(vresp.app_iter)
            if vresp.is_success and int(vresp.headers.get(
                    'X-Container-Object-Count', 0)) > 0:
                raise HTTPBadRequest(
                    'Delete all versions to delete container.',
                    request=req)
            elif not vresp.is_success and vresp.status_int != 404:
                raise HTTPInternalServerError(
                    'Error deleting versioned container')
            else:
                versions_req.method = 'DELETE'
                resp = versions_req.get_response(self.app)
                close_if_possible(resp.app_iter)
                if not is_success(resp.status_int) and resp.status_int != 404:
                    raise HTTPInternalServerError(
                        'Error deleting versioned container')

        app_resp = self._app_call(req.environ)

        start_response(self._response_status,
                       self._response_headers,
                       self._response_exc_info)
        return app_resp

    def enable_versioning(self, req, start_response):
        container_info = get_container_info(req.environ, self.app)

        # if container is already configured to use old style versioning,
        # we don't allow user to enable object versioning here. They must
        # choose which middleware to use, only one style of versioning
        # is supported for a given container
        versions_cont = container_info.get(
            'sysmeta', {}).get('versions-location')
        legacy_versions_cont = container_info.get('versions')
        if versions_cont or legacy_versions_cont:
            raise HTTPBadRequest(
                'Cannot enabled object versioning on a container '
                'that is already using the legacy versioned writes '
                'feature.',
                request=req)

        versions_cont = container_info.get(
            'sysmeta', {}).get('versions-container')
        is_enabled = config_true_value(
            req.headers[CLIENT_VERSIONS_ENABLED])

        req.headers[SYSMETA_VERSIONS_ENABLED] = is_enabled

        # TODO: a POST request to a primary container that doesn't exist
        # will fail, so we will create and delete the versions container
        # for no reason
        if config_true_value(is_enabled) and not versions_cont:
            (version, account, container, _) = req.split_path(3, 4, True)

            # Attempt to use same policy as primary container, otherwise
            # use default policy
            primary_policy_idx = container_info.get('storage_policy', None)
            if primary_policy_idx:
                hdrs = {'X-Storage-Policy': POLICIES[primary_policy_idx].name}
            else:
                if 'X-Storage-Policy' in req.headers:
                    hdrs = {'X-Storage-Policy':
                            req.headers['X-Storage-Policy']}
                else:
                    hdrs = {}
            hdrs['X-Backend-Allow-Reserved-Names'] = 'true'

            versions_cont = self._build_versions_container_name(container)
            versions_cont_path = "/%s/%s/%s" % (
                version, account, versions_cont)
            ver_cont_req = make_pre_authed_request(
                req.environ, path=wsgi_quote(versions_cont_path),
                method='PUT', headers=hdrs, swift_source='VW')
            resp = ver_cont_req.get_response(self.app)
            close_if_possible(resp.app_iter)
            if is_success(resp.status_int):
                req.headers[SYSMETA_VERSIONS_CONT] = wsgi_quote(versions_cont)
            else:
                raise HTTPInternalServerError(
                    'Error enabling object versioning')

        # make original request
        app_resp = self._app_call(req.environ)

        # if we just created a versions container but the original
        # request failed, delete the versions container
        # and let user retry later
        if not is_success(self._get_status_int()) and \
                SYSMETA_VERSIONS_CONT in req.headers:
            versions_cont_path = "/%s/%s/%s" % (
                version, account, versions_cont)
            ver_cont_req = make_pre_authed_request(
                req.environ, path=wsgi_quote(versions_cont_path),
                method='DELETE', headers=hdrs, swift_source='VW')

            # TODO: what if this one fails??
            resp = ver_cont_req.get_response(self.app)
            close_if_possible(resp.app_iter)

        if self._response_headers is None:
            self._response_headers = []
        for key, val in self._response_headers:
            if key.lower() == SYSMETA_VERSIONS_ENABLED:
                self._response_headers.extend([
                    (CLIENT_VERSIONS_ENABLED.title(), val)])

        start_response(self._response_status,
                       self._response_headers,
                       self._response_exc_info)
        return app_resp

    def _list_versions(self, req, start_response, location, primary_listing):
        # Only supports JSON listings
        req.environ['swift.format_listing'] = False
        if not req.accept.best_match(['application/json']):
            raise HTTPNotAcceptable(request=req)

        params = req.params
        if 'version_marker' in params:
            if 'marker' not in params:
                raise HTTPBadRequest('version_marker param requires marker')

            if params['version_marker'] != 'null':
                try:
                    ts = Timestamp(params.pop('version_marker'))
                except ValueError:
                    raise HTTPBadRequest('invalid version_marker param')

                params['marker'] = self._build_versions_object_name(
                    params['marker'], ts)
        elif 'marker' in params:
            params['marker'] = self._build_versions_object_prefix(
                params['marker']) + ':'  # just past all numbers

        delim = params.get('delimiter', '')
        # Exclude the set of chars used in version_id from user delimiters
        if set(delim).intersection('0123456789.%s' % RESERVED_STR):
            raise HTTPBadRequest('invalid delimiter param')

        null_listing = []
        subdir_set = set()
        current_versions = {}
        for item in primary_listing:
            if 'name' not in item:
                subdir_set.add(item['subdir'])
            else:
                if item.get('version_symlink'):
                    path = wsgi_to_str(wsgi_unquote(bytes_to_wsgi(
                        item['symlink_path'].encode('utf-8'))))
                    current_versions[path] = item
                else:
                    null_listing.append(dict(
                        item, version_id='null', is_latest=True))

        account = req.split_path(3, 3, True)[1]
        versions_req = make_pre_authed_request(
            req.environ, method='GET', swift_source='VW',
            path=wsgi_quote('/v1/%s/%s' % (account, location)),
            headers={'X-Backend-Allow-Reserved-Names': 'true'},
        )
        # NB: Not using self._build_versions_object_name here because
        # we don't want to bookend the prefix with RESERVED_NAME as user
        # could be using just part of object name as the prefix.
        if 'prefix' in params:
            params['prefix'] = get_reserved_name(params['prefix'])

        # NB: no end_marker support (yet)
        versions_req.params = {
            k: params.get(k, '')
            for k in ('prefix', 'marker', 'limit', 'delimiter', 'reverse')}
        versions_resp = versions_req.get_response(self.app)

        if versions_resp.status_int == HTTP_NOT_FOUND:
            subdir_listing = [{'subdir': s} for s in subdir_set]
            broken_listing = []
            for item in current_versions.values():
                linked_name = wsgi_to_str(wsgi_unquote(bytes_to_wsgi(
                    item['symlink_path'].encode('utf8')))).split('/', 4)[-1]
                name, ts = self._split_version_from_name(linked_name)
                if ts is None:
                    continue
                broken_listing.append({
                    'name': name.decode('utf8') if six.PY2 else name,
                    'is_latest': True,
                    'version_id': ts.internal,
                    'content_type': item['content_type'],
                    'bytes': item['bytes'],
                    'hash': item['hash'],
                    'last_modified': item['last_modified'],
                })
            body = build_listing(
                null_listing, subdir_listing, broken_listing,
                reverse=config_true_value(params.get('reverse', 'no')))
            self.update_content_length(len(body))
            app_resp = [body]
            close_if_possible(versions_resp.app_iter)
        elif is_success(versions_resp.status_int):
            try:
                listing = json.loads(versions_resp.body)
            except ValueError:
                app_resp = [body]
            else:
                versions_listing = []
                latest_del_marker = set()
                for item in listing:
                    if 'name' not in item:
                        # remove reserved chars from subdir
                        subdir = split_reserved_name(item['subdir'])[0]
                        subdir_set.add(subdir)
                    else:
                        name, ts = self._split_version_from_name(item['name'])
                        if ts is None:
                            continue
                        path = '/v1/%s/%s/%s' % (
                            wsgi_to_str(account),
                            wsgi_to_str(location),
                            item['name'].encode('utf8')
                            if six.PY2 else item['name'])

                        if path in current_versions:
                            item['is_latest'] = True
                            del current_versions[path]
                        elif (item['content_type'] ==
                              DELETE_MARKER_CONTENT_TYPE
                              and name not in latest_del_marker):
                            item['is_latest'] = True
                            latest_del_marker.add(name)
                        else:
                            item['is_latest'] = False

                        item['name'] = name
                        item['version_id'] = ts.internal
                        versions_listing.append(item)

                subdir_listing = [{'subdir': s} for s in subdir_set]
                broken_listing = []
                for item in current_versions.values():
                    link_path = wsgi_to_str(wsgi_unquote(bytes_to_wsgi(
                        item['symlink_path'].encode('utf-8'))))
                    name, ts = self._split_version_from_name(
                        link_path.split('/', 1)[1])
                    if ts is None:
                        continue
                    broken_listing.append({
                        'name': name.decode('utf8') if six.PY2 else name,
                        'is_latest': True,
                        'version_id': ts.internal,
                        'content_type': item['content_type'],
                        'bytes': item['bytes'],
                        'hash': item['hash'],
                        'last_modified': item['last_modified'],
                    })

                body = build_listing(
                    null_listing, versions_listing,
                    subdir_listing, broken_listing,
                    reverse=config_true_value(params.get('reverse', 'no')))
                self.update_content_length(len(body))
                app_resp = [body]
        else:
            return versions_resp(versions_req.environ, start_response)

        start_response(self._response_status,
                       self._response_headers,
                       self._response_exc_info)
        return app_resp


class ObjectVersioningMiddleware(object):

    def __init__(self, app, conf):
        self.app = app
        self.conf = conf
        self.logger = get_logger(conf, log_route='object_versioning')

    def filter_reserved(self, listing):
        new_listing = []
        for entry in list(listing):
            for key in ('name', 'subdir'):
                if RESERVED_STR not in entry.get(key, ''):
                    new_listing.append(entry)
        return new_listing

    def container_request(self, req, start_response):
        vw_ctx = ContainerContext(self.app, self.logger)
        if req.method in ('PUT', 'POST') and \
                CLIENT_VERSIONS_ENABLED in req.headers:
            return vw_ctx.enable_versioning(req, start_response)
        elif req.method == 'DELETE':
            return vw_ctx.handle_delete(req, start_response)

        # send request and translate sysmeta headers from response
        return vw_ctx.handle_request(req, start_response)

    def object_request(self, req, api_version, account, container, obj):
        """
        Handle request for object resource.

        Note that account, container, obj should be unquoted by caller
        if the url path is under url encoding (e.g. %FF)

        :param req: swift.common.swob.Request instance
        :param api_version: should be v1 unless swift bumps api version
        :param account: account name string
        :param container: container name string
        :param object: object name string
        """
        resp = None
        container_info = get_container_info(
            req.environ, self.app)

        versions_cont = container_info.get(
            'sysmeta', {}).get('versions-container', '')
        is_enabled = config_true_value(container_info.get(
            'sysmeta', {}).get('versions-enabled'))

        if versions_cont:
            versions_cont = wsgi_unquote(str_to_wsgi(
                versions_cont)).split('/')[0]

        if req.params.get('version-id'):
            vw_ctx = ObjectContext(self.app, self.logger)
            resp = vw_ctx.handle_versioned_request(
                req, versions_cont, api_version, account, container, obj,
                is_enabled, req.params['version-id'])
        elif versions_cont:
            # handle object request for a enabled versioned container
            vw_ctx = ObjectContext(self.app, self.logger)
            resp = vw_ctx.handle_request(
                req, versions_cont, api_version, account, container, obj,
                is_enabled)

        if resp:
            return resp
        else:
            return self.app

    def __call__(self, env, start_response):
        req = Request(env)
        try:
            (api_version, account, container, obj) = req.split_path(3, 4, True)
        except ValueError:
            return self.app(env, start_response)

        try:
            if container and not obj:
                return self.container_request(req, start_response)
            elif obj:
                return self.object_request(
                    req, api_version, account, container,
                    obj)(env, start_response)
        except HTTPException as error_response:
            return error_response(env, start_response)

        return self.app(env, start_response)