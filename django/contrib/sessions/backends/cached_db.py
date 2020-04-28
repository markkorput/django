"""
Cached, database-backed sessions.
"""

from django.conf import settings
from django.contrib.sessions.backends.db import SessionStore as DBStore
from django.core.cache import caches

KEY_PREFIX = "django.contrib.sessions.cached_db"


class SessionStore(DBStore):
    """
    Implement cached, database backed sessions.
    """
    cache_key_prefix = KEY_PREFIX

    def __init__(self, session_key=None):
        self._cache = caches[settings.SESSION_CACHE_ALIAS]
        super().__init__(session_key)

    @property
    def cache_key(self):
        session_key = self.get_backend_key(self._get_or_create_session_key())
        return self.cache_key_prefix + session_key

    def load(self):
        """
        Return cashed data if present, otherwise call DBStore.load()
        and cache the result if any data is returned.
        """
        try:
            data = self._cache.get(self.cache_key)
        except Exception:
            # Some backends (e.g. memcache) raise an exception on invalid
            # cache keys. If this happens, reset the session. See #17810.
            data = None

        if not data is None:
            return data

        s = super().load()

        if not s:
            return {}

        data = self.decode(s.session_data)
        self._cache.set(self.cache_key, data, self.get_expiry_age(expiry=s.expire_date))
        return data

    def exists(self, session_key):
        hashed_session_key = self.get_backend_key(session_key)
        if hashed_session_key and (self.cache_key_prefix + hashed_session_key) in self._cache:
            return True
        return super().exists(session_key)

    def save(self, must_create=False):
        super().save(must_create)
        self._cache.set(self.cache_key, self._session, self.get_expiry_age())

    def delete(self, session_key=None):
        session_key = self.get_backend_key(session_key)
        super().delete(session_key)
        if session_key is None:
            if self.session_key is None:
                return
            session_key = self.get_backend_key(self.session_key)
        self._cache.delete(self.cache_key_prefix + session_key)

    def flush(self):
        """
        Remove the current session data from the database and regenerate the
        key.
        """
        self.clear()
        self.delete(self.session_key)
        self._session_key = None
