"""HTTP Handlers for the hub server"""

# Copyright (c) Jupyter Development Team.
# Distributed under the terms of the Modified BSD License.

import re
from datetime import datetime
try:
    # py3
    from http.client import responses
except ImportError:
    from httplib import responses

from jinja2 import TemplateNotFound

from tornado.log import app_log
from tornado.httputil import url_concat
from tornado.web import RequestHandler
from tornado import gen, web

from .. import orm
from ..spawner import LocalProcessSpawner
from ..utils import url_path_join

# pattern for the authentication token header
auth_header_pat = re.compile(r'^token\s+([^\s]+)$')


class BaseHandler(RequestHandler):
    """Base Handler class with access to common methods and properties."""

    @property
    def log(self):
        """I can't seem to avoid typing self.log"""
        return self.settings.get('log', app_log)

    @property
    def config(self):
        return self.settings.get('config', None)

    @property
    def base_url(self):
        return self.settings.get('base_url', '/')

    @property
    def db(self):
        return self.settings['db']

    @property
    def hub(self):
        return self.settings['hub']
    
    @property
    def proxy(self):
        return self.settings['proxy']
    
    @property
    def authenticator(self):
        return self.settings.get('authenticator', None)

    #---------------------------------------------------------------
    # Login and cookie-related
    #---------------------------------------------------------------

    @property
    def admin_users(self):
        return self.settings.setdefault('admin_users', set())

    def get_current_user_token(self):
        """get_current_user from Authorization header token"""
        auth_header = self.request.headers.get('Authorization', '')
        match = auth_header_pat.match(auth_header)
        if not match:
            return None
        token = match.group(1)
        orm_token = orm.APIToken.find(self.db, token)
        if orm_token is None:
            return None
        else:
            user = orm_token.user
            user.last_activity = datetime.utcnow()
            return user

    def get_current_user_cookie(self):
        """get_current_user from a cookie token"""
        token = self.get_cookie(self.hub.server.cookie_name, None)
        if token:
            cookie_token = orm.CookieToken.find(self.db, token)
            if cookie_token:
                return cookie_token.user
            else:
                # have cookie, but it's not valid. Clear it and start over.
                self.clear_cookie(self.hub.server.cookie_name, path=self.hub.server.base_url)

    def get_current_user(self):
        """get current username"""
        user = self.get_current_user_token()
        if user is not None:
            return user
        return self.get_current_user_cookie()
    
    def find_user(self, name):
        """Get a user by name
        
        return None if no such user
        """
        return orm.User.find(self.db, name)

    def user_from_username(self, username):
        """Get ORM User for username"""
        user = self.find_user(username)
        if user is None:
            user = orm.User(name=username)
            self.db.add(user)
            self.db.commit()
        return user
    
    def clear_login_cookie(self):
        user = self.get_current_user()
        if user and user.server:
            self.clear_cookie(user.server.cookie_name, path=user.server.base_url)
        self.clear_cookie(self.hub.server.cookie_name, path=self.hub.server.base_url)

    def set_login_cookie(self, user):
        """Set login cookies for the Hub and single-user server."""
        # create and set a new cookie token for the single-user server
        if user.server:
            cookie_token = user.new_cookie_token()
            self.db.add(cookie_token)
            self.db.commit()
            self.set_cookie(
                user.server.cookie_name,
                cookie_token.token,
                path=user.server.base_url,
            )
        
        # create and set a new cookie token for the hub
        if not self.get_current_user_cookie():
            cookie_token = user.new_cookie_token()
            self.db.add(cookie_token)
            self.db.commit()
            self.set_cookie(
                self.hub.server.cookie_name,
                cookie_token.token,
                path=self.hub.server.base_url)
    
    @gen.coroutine
    def authenticate(self, data):
        auth = self.authenticator
        if auth is not None:
            result = yield auth.authenticate(self, data)
            raise gen.Return(result)
        else:
            self.log.error("No authentication function, login is impossible!")


    #---------------------------------------------------------------
    # spawning-related
    #---------------------------------------------------------------

    @property
    def spawner_class(self):
        return self.settings.get('spawner_class', LocalProcessSpawner)

    @gen.coroutine
    def spawn_single_user(self, user):
        yield user.spawn(
            spawner_class=self.spawner_class,
            base_url=self.base_url,
            hub=self.hub,
            config=self.config,
        )
        yield self.proxy.add_user(user)
        user.spawner.add_poll_callback(self.user_stopped, user)
        raise gen.Return(user)
    
    @gen.coroutine
    def user_stopped(self, user):
        status = yield user.spawner.poll()
        self.log.warn("User %s server stopped, with exit code: %s",
            user.name, status,
        )
        yield self.proxy.delete_user(user)
        yield user.stop()
    
    @gen.coroutine
    def stop_single_user(self, user):
        yield self.proxy.delete_user(user)
        yield user.stop()

    #---------------------------------------------------------------
    # template rendering
    #---------------------------------------------------------------

    def get_template(self, name):
        """Return the jinja template object for a given name"""
        return self.settings['jinja2_env'].get_template(name)

    def render_template(self, name, **ns):
        ns.update(self.template_namespace)
        template = self.get_template(name)
        return template.render(**ns)

    @property
    def template_namespace(self):
        user = self.get_current_user()
        return dict(
            base_url=self.hub.server.base_url,
            user=user,
            login_url=self.settings['login_url'],
            logout_url=self.settings['logout_url'],
            static_url=self.static_url,
        )

    def write_error(self, status_code, **kwargs):
        """render custom error pages"""
        exc_info = kwargs.get('exc_info')
        message = ''
        status_message = responses.get(status_code, 'Unknown HTTP Error')
        if exc_info:
            exception = exc_info[1]
            # get the custom message, if defined
            try:
                message = exception.log_message % exception.args
            except Exception:
                pass

            # construct the custom reason, if defined
            reason = getattr(exception, 'reason', '')
            if reason:
                status_message = reason

        # build template namespace
        ns = dict(
            status_code=status_code,
            status_message=status_message,
            message=message,
            exception=exception,
        )

        self.set_header('Content-Type', 'text/html')
        # render the template
        try:
            html = self.render_template('%s.html' % status_code, **ns)
        except TemplateNotFound:
            self.log.debug("No template for %d", status_code)
            html = self.render_template('error.html', **ns)

        self.write(html)


class Template404(BaseHandler):
    """Render our 404 template"""
    def prepare(self):
        raise web.HTTPError(404)


class PrefixRedirectHandler(BaseHandler):
    """Redirect anything outside a prefix inside.
    
    Redirects /foo to /prefix/foo, etc.
    """
    def get(self):
        self.redirect(url_path_join(
            self.hub.server.base_url, self.request.path,
        ), permanent=False)

class UserSpawnHandler(BaseHandler):
    """Requests to /user/name handled by the Hub
    should result in spawning the single-user server and
    being redirected to the original.
    """
    @gen.coroutine
    def get(self, name):
        current_user = self.get_current_user()
        if current_user and current_user.name == name:
            # logged in, spawn the server
            if current_user.spawner:
                status = yield current_user.spawner.poll()
                if status is not None:
                    yield self.spawn_single_user(current_user)
            else:
                yield self.spawn_single_user(current_user)
            # set login cookie anew
            self.set_login_cookie(current_user)
            without_prefix = self.request.path[len(self.hub.server.base_url):]
            if not without_prefix.startswith('/'):
                without_prefix = '/' + without_prefix
            self.redirect(without_prefix)
        else:
            # not logged in to the right user,
            # clear any cookies and reload (will redirect to login)
            self.clear_login_cookie()
            self.redirect(url_concat(
                self.settings['login_url'],
                {'next': self.request.path,
            }))

default_handlers = [
    (r'/user/([^/]+)/?.*', UserSpawnHandler),
]
