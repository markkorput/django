import base64
import hashlib
import logging
import string
import warnings
from datetime import datetime, timedelta

from django.conf import settings
from django.contrib.sessions.exceptions import SuspiciousSession
from django.core import signing
from django.core.exceptions import SuspiciousOperation
from django.utils import timezone
from django.utils.crypto import (
    constant_time_compare, get_random_string, salted_hmac,
)
from django.utils.deprecation import RemovedInDjango40Warning
from django.utils.module_loading import import_string
from django.utils.translation import LANGUAGE_SESSION_KEY

# session_key should not be case sensitive because some backends can store it
# on case insensitive file systems.
VALID_KEY_CHARS = string.ascii_lowercase + string.digits

# the delimiter cannot be a member of VALID_KEY_CHARS or a file directory
# component. It can only be a single character.
SESSION_KEY_DELIMITER = '$'
# this should be available in hashlib
SESSION_HASHING_ALGORITHM = 'sha256'
SESSION_HASHED_KEY_PREFIX = SESSION_HASHING_ALGORITHM + SESSION_KEY_DELIMITER

class CreateError(Exception):
    """
    Used internally as a consistent exception type to catch from save (see the
    docstring for SessionBase.save() for details).
    """
    pass


class UpdateError(Exception):
    """
    Occurs if Django tries to update a session that was deleted.
    """
    pass


class SessionBase:
    """
    Base class for all Session classes.
    """
    TEST_COOKIE_NAME = 'testcookie'
    TEST_COOKIE_VALUE = 'worked'

    __not_given = object()

    def __init__(self, session_key=None):
        self._session_key = session_key
        self.accessed = False
        self.modified = False
        self.serializer = import_string(settings.SESSION_SERIALIZER)

    def __contains__(self, key):
        return key in self._session

    def __getitem__(self, key):
        if key == LANGUAGE_SESSION_KEY:
            warnings.warn(
                'The user language will no longer be stored in '
                'request.session in Django 4.0. Read it from '
                'request.COOKIES[settings.LANGUAGE_COOKIE_NAME] instead.',
                RemovedInDjango40Warning, stacklevel=2,
            )
        return self._session[key]

    def __setitem__(self, key, value):
        self._session[key] = value
        self.modified = True

    def __delitem__(self, key):
        del self._session[key]
        self.modified = True

    @property
    def key_salt(self):
        return 'django.contrib.sessions.' + self.__class__.__qualname__

    def get(self, key, default=None):
        return self._session.get(key, default)

    def pop(self, key, default=__not_given):
        self.modified = self.modified or key in self._session
        args = () if default is self.__not_given else (default,)
        return self._session.pop(key, *args)

    def setdefault(self, key, value):
        if key in self._session:
            return self._session[key]
        else:
            self.modified = True
            self._session[key] = value
            return value

    def set_test_cookie(self):
        self[self.TEST_COOKIE_NAME] = self.TEST_COOKIE_VALUE

    def test_cookie_worked(self):
        return self.get(self.TEST_COOKIE_NAME) == self.TEST_COOKIE_VALUE

    def delete_test_cookie(self):
        del self[self.TEST_COOKIE_NAME]

    def _hash(self, value):
        # RemovedInDjango40Warning: pre-Django 3.1 format will be invalid.
        key_salt = "django.contrib.sessions" + self.__class__.__name__
        return salted_hmac(key_salt, value).hexdigest()

    def encode(self, session_dict):
        "Return the given session dictionary serialized and encoded as a string."
        return signing.dumps(
            session_dict, salt=self.key_salt, serializer=self.serializer,
            compress=True,
        )

    def decode(self, session_data):
        try:
            return signing.loads(session_data, salt=self.key_salt, serializer=self.serializer)
        # RemovedInDjango40Warning: when the deprecation ends, handle here
        # exceptions similar to what _legacy_decode() does now.
        except Exception:
            return self._legacy_decode(session_data)

    def _legacy_decode(self, session_data):
        # RemovedInDjango40Warning: pre-Django 3.1 format will be invalid.
        encoded_data = base64.b64decode(session_data.encode('ascii'))
        try:
            # could produce ValueError if there is no ':'
            hash, serialized = encoded_data.split(b':', 1)
            expected_hash = self._hash(serialized)
            if not constant_time_compare(hash.decode(), expected_hash):
                raise SuspiciousSession("Session data corrupted")
            else:
                return self.serializer().loads(serialized)
        except Exception as e:
            # ValueError, SuspiciousOperation, unpickling exceptions. If any of
            # these happen, just return an empty dictionary (an empty session).
            if isinstance(e, SuspiciousOperation):
                logger = logging.getLogger('django.security.%s' % e.__class__.__name__)
                logger.warning(str(e))
            return {}

    def update(self, dict_):
        self._session.update(dict_)
        self.modified = True

    def has_key(self, key):
        return key in self._session

    def keys(self):
        return self._session.keys()

    def values(self):
        return self._session.values()

    def items(self):
        return self._session.items()

    def clear(self):
        # To avoid unnecessary persistent storage accesses, we set up the
        # internals directly (loading data wastes time, since we are going to
        # set it to an empty dict anyway).
        self._session_cache = {}
        self.accessed = True
        self.modified = True

    def is_empty(self):
        "Return True when there is no session_key and the session is empty."
        try:
            return not self._session_key and not self._session_cache
        except AttributeError:
            return True

    def _get_new_session_key(self):
        "Return session key that isn't being used."
        while True:
            session_key = get_random_string(32, VALID_KEY_CHARS)
            if not self.exists(session_key):
                return session_key

    def _get_or_create_session_key(self):
        if self._session_key is None:
            self._session_key = self._get_new_session_key()
        return self._session_key

    def _validate_session_key(self, key):
        """
        Key must be truthy and at least 8 characters long. 8 characters is an
        arbitrary lower bound for some minimal key security.
        """
        return key and len(key) >= 8

    def _get_session_key(self):
        return self.__session_key

    def _set_session_key(self, value):
        """
        Validate session key on assignment. Invalid values will set to None.
        """
        if self._validate_session_key(value):
            self.__session_key = value
        else:
            self.__session_key = None

    session_key = property(_get_session_key)
    _session_key = property(_get_session_key, _set_session_key)

    def _get_session(self, no_load=False):
        """
        Lazily load session from storage (unless "no_load" is True, when only
        an empty dict is stored) and store it in the current instance.
        """
        self.accessed = True
        try:
            return self._session_cache
        except AttributeError:
            if self.session_key is None or no_load:
                self._session_cache = {}
            else:
                self._session_cache = self.load()
        return self._session_cache

    _session = property(_get_session)

    def get_session_cookie_age(self):
        return settings.SESSION_COOKIE_AGE

    def get_expiry_age(self, **kwargs):
        """Get the number of seconds until the session expires.

        Optionally, this function accepts `modification` and `expiry` keyword
        arguments specifying the modification and expiry of the session.
        """
        try:
            modification = kwargs['modification']
        except KeyError:
            modification = timezone.now()
        # Make the difference between "expiry=None passed in kwargs" and
        # "expiry not passed in kwargs", in order to guarantee not to trigger
        # self.load() when expiry is provided.
        try:
            expiry = kwargs['expiry']
        except KeyError:
            expiry = self.get('_session_expiry')

        if not expiry:   # Checks both None and 0 cases
            return self.get_session_cookie_age()
        if not isinstance(expiry, datetime):
            return expiry
        delta = expiry - modification
        return delta.days * 86400 + delta.seconds

    def get_expiry_date(self, **kwargs):
        """Get session the expiry date (as a datetime object).

        Optionally, this function accepts `modification` and `expiry` keyword
        arguments specifying the modification and expiry of the session.
        """
        try:
            modification = kwargs['modification']
        except KeyError:
            modification = timezone.now()
        # Same comment as in get_expiry_age
        try:
            expiry = kwargs['expiry']
        except KeyError:
            expiry = self.get('_session_expiry')

        if isinstance(expiry, datetime):
            return expiry
        expiry = expiry or self.get_session_cookie_age()
        return modification + timedelta(seconds=expiry)

    def set_expiry(self, value):
        """
        Set a custom expiration for the session. ``value`` can be an integer,
        a Python ``datetime`` or ``timedelta`` object or ``None``.

        If ``value`` is an integer, the session will expire after that many
        seconds of inactivity. If set to ``0`` then the session will expire on
        browser close.

        If ``value`` is a ``datetime`` or ``timedelta`` object, the session
        will expire at that specific future time.

        If ``value`` is ``None``, the session uses the global session expiry
        policy.
        """
        if value is None:
            # Remove any custom expiration for this session.
            try:
                del self['_session_expiry']
            except KeyError:
                pass
            return
        if isinstance(value, timedelta):
            value = timezone.now() + value
        self['_session_expiry'] = value

    def get_expire_at_browser_close(self):
        """
        Return ``True`` if the session is set to expire when the browser
        closes, and ``False`` if there's an expiry date. Use
        ``get_expiry_date()`` or ``get_expiry_age()`` to find the actual expiry
        date/age, if there is one.
        """
        if self.get('_session_expiry') is None:
            return settings.SESSION_EXPIRE_AT_BROWSER_CLOSE
        return self.get('_session_expiry') == 0

    def flush(self):
        """
        Remove the current session data from the database and regenerate the
        key.
        """
        self.clear()
        self.delete()
        self._session_key = None

    def cycle_key(self):
        """
        Create a new session key, while retaining the current session data.
        """
        data = self._session
        key = self.session_key
        self.create()
        self._session_cache = data
        if key:
            self.delete(key)

    # Methods that child classes must implement.

    def exists(self, session_key):
        """
        Return True if the given session_key already exists.
        """
        raise NotImplementedError('subclasses of SessionBase must provide an exists() method')

    def create(self):
        """
        Create a new session instance. Guaranteed to create a new object with
        a unique key and will have saved the result once (with empty data)
        before the method returns.
        """
        raise NotImplementedError('subclasses of SessionBase must provide a create() method')

    def save(self, must_create=False):
        """
        Save the session data. If 'must_create' is True, create a new session
        object (or raise CreateError). Otherwise, only update an existing
        object and don't create one (raise UpdateError if needed).
        """
        raise NotImplementedError('subclasses of SessionBase must provide a save() method')

    def delete(self, session_key=None):
        """
        Delete the session data under this key. If the key is None, use the
        current session key value.
        """
        raise NotImplementedError('subclasses of SessionBase must provide a delete() method')

    def load(self):
        """
        Load the session data and return a dictionary.
        """
        raise NotImplementedError('subclasses of SessionBase must provide a load() method')

    @classmethod
    def clear_expired(cls):
        """
        Remove expired sessions from the session store.

        If this operation isn't possible on a given backend, it should raise
        NotImplementedError. If it isn't necessary, because the backend has
        a built-in expiration mechanism, it should be a no-op.
        """
        raise NotImplementedError('This backend does not support clear_expired().')


class HashingSessionBase(SessionBase):
    _algorithm = getattr(hashlib, SESSION_HASHING_ALGORITHM)

    """
    Base class for all Session classes since introducing session-key hashing.
    """
    @classmethod
    def get_serializer(cls):
        return import_string(settings.SESSION_SERIALIZER)

    @classmethod
    def get_key_salt(cls):
        return 'django.contrib.sessions.' + cls.__qualname__

    @classmethod
    def _encode(cls, session_dict):
        "Return the given session dictionary serialized and encoded as a string."
        return signing.dumps(
            session_dict, salt=cls.get_key_salt(), serializer=cls.get_serializer(),
            compress=True,
        )

    def encode(self, session_dict):
        return self._encode(session_dict)

    @classmethod
    def __hash(cls, value):
        # RemovedInDjango40Warning: pre-Django 3.1 format will be invalid.
        key_salt = "django.contrib.sessions" + cls.__name__
        return salted_hmac(key_salt, value).hexdigest()

    @classmethod
    def __legacy_decode(cls, session_data):
        # RemovedInDjango40Warning: pre-Django 3.1 format will be invalid.
        encoded_data = base64.b64decode(session_data.encode('ascii'))
        try:
            # could produce ValueError if there is no ':'
            hash, serialized = encoded_data.split(b':', 1)
            expected_hash = cls.__hash(serialized)
            if not constant_time_compare(hash.decode(), expected_hash):
                raise SuspiciousSession("Session data corrupted")
            else:
                ser = cls.get_serializer()
                return ser().loads(serialized)
        except Exception as e:
            # ValueError, SuspiciousOperation, unpickling exceptions. If any of
            # these happen, just return an empty dictionary (an empty session).
            if isinstance(e, SuspiciousOperation):
                logger = logging.getLogger('django.security.%s' % e.__class__.__name__)
                logger.warning(str(e))
            return {}

    def _legacy_decode(self, session_data):
        self.__legacy_decode(session_data)

    @classmethod
    def _decode(cls, session_data):
        try:
            return signing.loads(session_data, salt=cls.get_key_salt(), serializer=cls.get_serializer())
        # RemovedInDjango40Warning: when the deprecation ends, handle here
        # exceptions similar to what _legacy_decode() does now.
        except Exception:
            return cls.__legacy_decode(session_data)

    def decode(self, session_data):
        return self._decode(session_data)

    def _get_new_session_key(self):
        """
        Return new unique session key. If SESSION_STORE_KEY_HASH is False, the
        key is a 32-character string. If SESSION_STORE_KEY_HASH is True the key
        is a frontend_key consisting of a hashing algorithm prefix followed by
        a 32-character session key.
        """
        while True:
            # session_key
            keys = [get_random_string(32, VALID_KEY_CHARS)]
            # also consider hashed frontend_key if hashing is enabled
            if settings.SESSION_STORE_KEY_HASH:
                hashed_frontend_key = SESSION_HASHED_KEY_PREFIX + str(keys[0])
                keys.append(hashed_frontend_key)

            # only if the key(s) don't already exist
            if next((key for key in keys if self.exists(key)), None) == None:
                # return the relevant key
                return keys[-1]

    def _get_or_create_session_key(self):
        """
        Initialise current session with a new frontend key if it
        doesn't have one yet and return corresponding backend key
        """
        if self._session_key is None:
            self._session_key = self._get_new_session_key()
        return self.get_backend_key(self._session_key)

    def _validate_session_key(self, frontend_key):
        """
        Key must be truthy and at least 8 characters long and
        in the correct format if hashing is required.
        """
        valid = frontend_key != None and len(frontend_key) >= 8
        if settings.SESSION_REQUIRE_KEY_HASH:
            valid &= self._has_hash_prefix(frontend_key)
        return valid

    @staticmethod
    def get_session_cookie_age_setting():
        return settings.SESSION_COOKIE_AGE

    @classmethod
    def _get_expiry_date(cls, session_data, **kwargs):
        """Get session the expiry date (as a datetime object).

        Optionally, this function accepts `modification` and `expiry` keyword
        arguments specifying the modification and expiry of the session.
        """
        try:
            modification = kwargs['modification']
        except KeyError:
            modification = timezone.now()
        # Same comment as in get_expiry_age
        try:
            expiry = kwargs['expiry']
        except KeyError:
            # expiry = self.get('_session_expiry')
            expiry = session_data['_session_expiry'] if '_session_expiry' in session_data else None

        if isinstance(expiry, datetime):
            return expiry
        expiry = expiry or cls.get_session_cookie_age_setting()
        return modification + timedelta(seconds=expiry)

    def get_expiry_date(self, **kwargs):
        return self._get_expiry_date(self._session, **kwargs)

    @classmethod
    def get_backend_key(cls, frontend_key):
        """
        Return backend version of the given frontend key.
        Applies hashing if required by settings.
        """
        if frontend_key is None:
            return None
        if not cls._has_hash_prefix(frontend_key):
            return frontend_key

        hashless_key = frontend_key[len(SESSION_HASHED_KEY_PREFIX):]

        return cls._algorithm(hashless_key.encode('ascii')).hexdigest()

    @staticmethod
    def _has_hash_prefix(frontend_key):
        """Return True when the session_key is hashed in the backend."""
        return str(frontend_key).startswith(SESSION_HASHED_KEY_PREFIX)

    # SessionBase methods

    def exists(self, frontend_key):
        """
        Return ``True`` if a session identified by the given frontend_key
        exists. If 'frontend_key' is None, the current session's session_key
        will be used.
        """
        if frontend_key is None:
            frontend_key = self._get_or_create_session_key()

        backend_key = self.get_backend_key(frontend_key)
        return self._exists(backend_key)

    def create(self):
        """
        Create a new session instance. Guaranteed to create a new object with
        a unique key and will have saved the result once (with empty data)
        before the method returns.
        """
        while True:
            self._session_key = self._get_new_session_key()
            try:
                self.save(must_create=True)
            except CreateError:
                continue
            self.modified = True
            return

    def save(self, must_create=False):
        """
        Save the current session. if 'must_create' is ``True``
        or the current session does not have a session_key yet
        it will create a new session record. Otherwise it will
        update the existing record identified by the self.session_key.
        """
        if self.session_key is None:
            return self.create()
        session_data = self._get_session(no_load=must_create)
        return self._save(self.get_backend_key(self.session_key), session_data, must_create=must_create)

    def delete(self, frontend_key=None):
        """
        Delete the session data under this key. If the key is None, use the
        current session key value.
        """
        if frontend_key is None and not self.session_key is None:
            frontend_key = self.session_key
        
        if not frontend_key is None:
            self._delete(self.get_backend_key(frontend_key))

    def load(self):
        """
        Load this session's data and return a dictionary.
        Return empty dictionary this session does not have a session
        or the session was not found.
        """
        frontend_key = self.session_key

        if frontend_key is None:
            return {}

        backend_key = self.get_backend_key(frontend_key)
        data = self._load_data(backend_key)

        # None is returned if session isn't found
        if data is None:
            self._session_key = None
            # only return None when there is no frontend_key
            return {}

        return data

    # Methods that child classes must implement.

    @classmethod
    def _exists(cls, backend_key):
        """
        Return True if a session for the given 'backend_key' already exists.
        """
        raise NotImplementedError('subclasses of SessionBase must provide a _exists() method')

    @classmethod
    def _load_data(cls, backend_key):
        """
        Load the session data for the session identified
        by 'backend_key' and return a dictionary.
        Return None if the session doesn't exists.
        """
        raise NotImplementedError('subclasses of SessionBase must provide a _load_data() method')

    @classmethod
    def _save(cls, backend_key, session_data, must_create=False):
        """
        Save the session data for the session identified by 'backend_key'.
        If 'must_create' is True, create a new session object (or raise
        CreateError). Otherwise, only update an existing object and don't
        create one (raise UpdateError if needed).
        """
        raise NotImplementedError('subclasses of SessionBase must provide a _save() method')

    @classmethod
    def _delete(cls, backend_key):
        """
        Delete the session identified by 'backend_key'.
        """
        raise NotImplementedError('subclasses of SessionBase must provide a _delete() method')

    @classmethod
    def clear_expired(cls):
        """
        Remove expired sessions from the session store.

        If this operation isn't possible on a given backend, it should raise
        NotImplementedError. If it isn't necessary, because the backend has
        a built-in expiration mechanism, it should be a no-op.
        """
        raise NotImplementedError('This backend does not support clear_expired().')

