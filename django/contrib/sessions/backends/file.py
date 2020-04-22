import datetime
import logging
import os
import shutil
import tempfile
from contextlib import suppress

from django.conf import settings
from django.contrib.sessions.backends.base import (
    VALID_KEY_CHARS, CreateError, SessionBase, UpdateError,
)
from django.contrib.sessions.exceptions import InvalidSessionKey
from django.core.exceptions import ImproperlyConfigured, SuspiciousOperation
from django.utils import timezone


class SessionStore(SessionBase):
    """
    Implement a file based session store.
    """
    def __init__(self, session_key=None):
        super().__init__(session_key)
        self._get_storage_path() # preload, cache and validate storage_path

    @staticmethod
    def _get_file_prefix():
        return settings.SESSION_COOKIE_NAME

    @classmethod
    def _get_storage_path(cls):
        try:
            return cls._storage_path
        except AttributeError:
            storage_path = getattr(settings, 'SESSION_FILE_PATH', None) or tempfile.gettempdir()
            # Make sure the storage path is valid.
            if not os.path.isdir(storage_path):
                raise ImproperlyConfigured(
                    "The session storage path %r doesn't exist. Please set your"
                    " SESSION_FILE_PATH setting to an existing directory in which"
                    " Django can store session data." % storage_path)

            cls._storage_path = storage_path
            return storage_path

    def _key_to_file(self, session_key=None):
        """
        Get the file associated with this session key.
        """
        if session_key is None:
            session_key = self._get_or_create_session_key()
        else:
            session_key = self.get_backend_key(session_key)

        return self._backend_key_to_file(session_key)

    @classmethod
    def _backend_key_to_file(cls, backend_key):
        # Make sure we're not vulnerable to directory traversal. Session keys
        # should always be md5s, so they should never contain directory
        # components.
        if not set(backend_key).issubset(VALID_KEY_CHARS):
            raise InvalidSessionKey(
                "Invalid characters in session key")

        return os.path.join(cls._get_storage_path(), settings.SESSION_COOKIE_NAME + backend_key)

    @staticmethod
    def _last_modification(file_path):
        """
        Return the modification time of the file storing the session's content.
        """
        modification = os.stat(file_path).st_mtime
        if settings.USE_TZ:
            modification = datetime.datetime.utcfromtimestamp(modification)
            return modification.replace(tzinfo=timezone.utc)
        return datetime.datetime.fromtimestamp(modification)

    def _expiry_date(self, session_data, file_path=None):
        """
        Return the expiry time of the file storing the session's content.
        """
        return session_data.get('_session_expiry') or (
            self._last_modification(file_path if file_path else self._key_to_file()) + datetime.timedelta(seconds=self.get_session_cookie_age())
        )

    @classmethod
    def _load_session_data(cls, file_path):
        """
        Return dict with session data from specified file,
        return empty dict if specified file doesn't exits,
        return False if the file contains invalid content.
        """
        file_data = None

        with open(file_path, encoding='ascii') as session_file:
            file_data = session_file.read()
        
        # Don't fail if there is no data in the session file.
        # We may have opened the empty placeholder file.
        if not file_data:
            return {}

        try:
            session_data = cls().decode(file_data)
        except (EOFError, SuspiciousOperation) as e:
            if isinstance(e, SuspiciousOperation):
                logger = logging.getLogger('django.security.%s' % e.__class__.__name__)
                logger.warning(str(e))
            return False

        return session_data

    def load(self):
        session_data = {}
        try:
            session_data = super().load()

            if session_data == None:
                session_data = {}

            elif session_data == False:
                self.create()
                session_data = {}

            else:
                # Remove expired sessions.
                expiry_age = self.get_expiry_age(expiry=self._expiry_date(session_data))
                if expiry_age <= 0:
                    session_data = {}
                    self.delete()
                    self.create()

        except (OSError, SuspiciousOperation):
            self._session_key = None

        return session_data

    @classmethod
    def _load_data(cls, backend_key):
        return cls._load_session_data(cls._backend_key_to_file(backend_key))

    @classmethod
    def _save(cls, backend_key, session_data, must_create=False):
        session_file_name = cls._backend_key_to_file(backend_key)

        try:
            # Make sure the file exists.  If it does not already exist, an
            # empty placeholder file is created.
            flags = os.O_WRONLY | getattr(os, 'O_BINARY', 0)
            if must_create:
                flags |= os.O_EXCL | os.O_CREAT
            fd = os.open(session_file_name, flags)
            os.close(fd)
        except FileNotFoundError:
            if not must_create:
                raise UpdateError
        except FileExistsError:
            if must_create:
                raise CreateError

        # Write the session file without interfering with other threads
        # or processes.  By writing to an atomically generated temporary
        # file and then using the atomic os.rename() to make the complete
        # file visible, we avoid having to lock the session file, while
        # still maintaining its integrity.
        #
        # Note: Locking the session file was explored, but rejected in part
        # because in order to be atomic and cross-platform, it required a
        # long-lived lock file for each session, doubling the number of
        # files in the session storage directory at any given time.  This
        # rename solution is cleaner and avoids any additional overhead
        # when reading the session data, which is the more common case
        # unless SESSION_SAVE_EVERY_REQUEST = True.
        #
        # See ticket #8616.
        dir, prefix = os.path.split(session_file_name)

        try:
            output_file_fd, output_file_name = tempfile.mkstemp(dir=dir, prefix=prefix + '_out_')
            renamed = False
            try:
                try:
                    os.write(output_file_fd, cls._encode(session_data).encode())
                finally:
                    os.close(output_file_fd)

                # This will atomically rename the file (os.rename) if the OS
                # supports it. Otherwise this will result in a shutil.copy2
                # and os.unlink (for example on Windows). See #9084.
                shutil.move(output_file_name, session_file_name)
                renamed = True
            finally:
                if not renamed:
                    os.unlink(output_file_name)
        except (EOFError, OSError):
            pass
        pass

    @classmethod
    def _exists(cls, backend_key):
        return os.path.exists(cls._backend_key_to_file(backend_key))

    @classmethod
    def _delete(cls, backend_key):
        SessionStore._delete_file(cls._backend_key_to_file(backend_key))

    @staticmethod
    def _delete_file(file_path):
        try:
            os.unlink(file_path)
        except OSError:
            pass

    def clean(self):
        pass

    @classmethod
    def clear_expired(cls):
        storage_path = cls._get_storage_path()
        file_prefix = cls._get_file_prefix()

        for session_file in os.listdir(storage_path):
            if not session_file.startswith(file_prefix):
                continue

            file_path = os.path.join(storage_path, session_file)
            session_data = cls._load_session_data(file_path)
            if session_data != False:
                expiry_age = cls().get_expiry_age(expiry=cls()._expiry_date(session_data, file_path=file_path))

                if expiry_age <= 0:
                    session_data = {}
                    cls._delete_file(file_path)
