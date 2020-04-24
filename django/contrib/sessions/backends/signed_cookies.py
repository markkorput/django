from django.contrib.sessions.backends.base import HashingSessionBase
from django.core import signing


class SessionStore(HashingSessionBase):
    # Even though this SessionStore doesn't perform key-hashing, it still inherits
    # from HashingSessionBase (and not from SessionBase directly) because
    # backends that don't inherit from HashingSessionBase will cause deprecation
    # warnings.

    @classmethod
    def _validate_frontend_key(cls, frontend_key):
        """
        Key must be truthy and at least 8 characters long.
        Skip HashingSessionBase's HASHING validation
        """
        return frontend_key and len(frontend_key) >= 8

    def _validate_session_key(self, frontend_key):
        # RemovedInDjango40Warning
        return self._validate_frontend_key(frontend_key)

    def load(self):
        """
        Load the data from the key itself instead of fetching from some
        external data store. Opposite of _get_session_key(), raise
        BadSignature if signature fails.
        """
        try:
            return signing.loads(
                self.frontend_key,
                serializer=self.serializer,
                # This doesn't handle non-default expiry dates, see #19201
                max_age=self.get_session_cookie_age(),
                salt='django.contrib.sessions.backends.signed_cookies',
            )
        except Exception:
            # BadSignature, ValueError, or unpickling exceptions. If any of
            # these happen, reset the session.
            self.create()
        return {}

    def create(self):
        """
        To create a new key, set the modified flag so that the cookie is set
        on the client for the current request.
        """
        self.modified = True

    def save(self, must_create=False):
        """
        To save, get the session key as a securely signed string and then set
        the modified flag so that the cookie is set on the client for the
        current request.
        """
        self._frontend_key = self._get_frontend_key()
        self.modified = True

    def exists(self, backend_key=None):
        """
        This method makes sense when you're talking to a shared resource, but
        it doesn't matter when you're storing the information in the client's
        cookie.
        """
        return False

    def delete(self, backend_key=None):
        """
        To delete, clear the session key and the underlying data structure
        and set the modified flag so that the cookie is set on the client for
        the current request.
        """
        self._frontend_key = ''
        self._session_cache = {}
        self.modified = True

    def cycle_key(self):
        """
        Keep the same data but with a new key. Call save() and it will
        automatically save a cookie with a new key at the end of the request.
        """
        self.save()

    def _get_session_key(self):
        # RemovedInDjango40Warning
        return self._get_frontend_key()

    def _get_frontend_key(self):
        """
        Instead of generating a random string, generate a secure url-safe
        base64-encoded string of data as our session key.
        """
        return signing.dumps(
            self._session, compress=True,
            salt='django.contrib.sessions.backends.signed_cookies',
            serializer=self.get_serializer())

    @classmethod
    def clear_expired(cls):
        pass
