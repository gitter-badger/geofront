""":mod:`geofront.backends.github` --- GitHub organization and key store
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

"""
import collections
import collections.abc
import contextlib
import io
import json
import logging
import urllib.request

from werkzeug.http import parse_options_header
from werkzeug.urls import url_encode, url_decode_stream
from werkzeug.wrappers import Request

from ..identity import Identity
from ..keystore import KeyStore, PublicKey
from ..team import AuthenticationError, Team
from ..util import typed


__all__ = {'GitHubOrganization', 'request'}


def request(access_token, url: str, method: str='GET', data: bytes=None):
    """Make a request to GitHub API, and then return the parsed JSON result.

    :param access_token: api access token string,
                         or :class:`~geofront.identity.Identity` instance
    :type access_token: :class:`str`, :class:`~geofront.identity.Identity`
    :param url: the api url to request
    :type url: :class:`str`
    :param method: an optional http method.  ``'GET'`` by default
    :type method: :class:`str`
    :param data: an optional content body
    :type data: :class:`bytes`

    """
    if isinstance(access_token, Identity):
        access_token = access_token.access_token
    req = urllib.request.Request(
        url,
        headers={
            'Authorization': 'token ' + access_token,
            'Accept': 'application/json'
        },
        method=method,
        data=data
    )
    with contextlib.closing(urllib.request.urlopen(req)) as response:
        content_type = response.headers.get('Content-Type')
        mimetype, options = parse_options_header(content_type)
        assert mimetype == 'application/json' or method == 'DELETE', \
            'Content-Type of {} is not application/json but {}'.format(
                url,
                content_type
            )
        charset = options.get('charset', 'utf-8')
        io_wrapper = io.TextIOWrapper(response, encoding=charset)
        logger = logging.getLogger(__name__ + '.request')
        if logger.isEnabledFor(logging.DEBUG):
            read = io_wrapper.read()
            logger.debug(
                'HTTP/%d.%d %d %s\n%s\n\n%s',
                response.version // 10,
                response.version % 10,
                response.status,
                response.reason,
                '\n'.join('{}: {}'.format(k, v)
                          for k, v in response.headers.items()),
                read
            )
            if method == 'DELETE':
                return
            return json.loads(read)
        else:
            if method == 'DELETE':
                io_wrapper.read()
                return
            return json.load(io_wrapper)


class GitHubOrganization(Team):
    """Authenticate team membership through GitHub, and authorize to
    access GitHub key store.

    :param client_id: github api client id
    :type client_id: :class:`str`
    :param client_secret: github api client secret
    :type client_secret: :class:`str`
    :param org_login: github org account name.  for example ``'spoqa'``
                      in https://github.com/spoqa
    :type org_login: :class:`str`

    """

    AUTHORIZE_URL = 'https://github.com/login/oauth/authorize'
    ACCESS_TOKEN_URL = 'https://github.com/login/oauth/access_token'
    USER_URL = 'https://api.github.com/user'
    ORGS_LIST_URL = 'https://api.github.com/user/orgs'

    @typed
    def __init__(self, client_id: str, client_secret: str, org_login: str):
        self.client_id = client_id
        self.client_secret = client_secret
        self.org_login = org_login

    @typed
    def request_authentication(self, auth_nonce: str, redirect_url: str) -> str:
        query = url_encode({
            'client_id': self.client_id,
            'redirect_uri': redirect_url,
            'scope': 'read:org,admin:public_key',
            'state': auth_nonce
        })
        authorize_url = '{}?{}'.format(self.AUTHORIZE_URL, query)
        return authorize_url

    @typed
    def authenticate(self,
                     auth_nonce: str,
                     requested_redirect_url: str,
                     wsgi_environ: collections.abc.Mapping) -> Identity:
        req = Request(wsgi_environ, populate_request=False, shallow=True)
        try:
            code = req.args['code']
            if req.args['state'] != auth_nonce:
                raise AuthenticationError()
        except KeyError:
            raise AuthenticationError()
        data = url_encode({
            'client_id': self.client_id,
            'client_secret': self.client_secret,
            'code': code,
            'redirect_uri': requested_redirect_url
        }).encode()
        response = urllib.request.urlopen(self.ACCESS_TOKEN_URL, data)
        content_type = response.headers['Content-Type']
        mimetype, options = parse_options_header(content_type)
        if mimetype == 'application/x-www-form-urlencoded':
            token_data = url_decode_stream(response)
        elif mimetype == 'application/json':
            charset = options.get('charset')
            token_data = json.load(io.TextIOWrapper(response, encoding=charset))
        else:
            response.close()
            raise AuthenticationError(
                '{} sent unsupported content type: {}'.format(
                    self.ACCESS_TOKEN_URL,
                    content_type
                )
            )
        response.close()
        user_data = request(token_data['access_token'], self.USER_URL)
        identity = Identity(
            type(self),
            user_data['login'],
            token_data['access_token']
        )
        if self.authorize(identity):
            return identity
        raise AuthenticationError(
            '@{} user is not a member of @{} organization'.format(
                user_data['login'],
                self.org_login
            )
        )

    def authorize(self, identity: Identity) -> bool:
        if not issubclass(identity.team_type, type(self)):
            return False
        try:
            response = request(identity, self.ORGS_LIST_URL)
        except IOError:
            return False
        if isinstance(response, collections.Mapping) and 'error' in response:
            return False
        return any(o['login'] == self.org_login for o in response)


class GitHubKeyStore(KeyStore):
    """Use GitHub account's public keys as key store."""

    LIST_URL = 'https://api.github.com/user/keys'
    DEREGISTER_URL = 'https://api.github.com/user/keys/{id}'

    @typed
    def register(self, identity: Identity, public_key: PublicKey):
        data = json.dumps({
            'title': public_key.comment,
            'key': str(public_key)
        })
        request(identity, self.LIST_URL, 'POST', data=data.encode())

    @typed
    def list_keys(self, identity: Identity) -> collections.abc.Set:
        keys = request(identity, self.LIST_URL)
        return {PublicKey.parse_line(key['key']) for key in keys}

    @typed
    def deregister(self, identity: Identity, public_key: PublicKey):
        keys = request(identity, self.LIST_URL)
        for key in keys:
            if PublicKey.parse_line(key['key']) == public_key:
                request(identity, self.DEREGISTER_URL.format(**key), 'DELETE')
                break
