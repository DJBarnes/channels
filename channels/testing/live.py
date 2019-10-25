import asyncio
import sys
import threading
import queue

from django.conf import settings
from django.db import connections
from django.test.testcases import LiveServerTestCase, LiveServerThread
from django.test.utils import modify_settings

from channels.routing import get_default_application
from channels.staticfiles import StaticFilesWrapper
from daphne.endpoints import build_endpoint_description_strings
from daphne.server import Server


class DaphneLiveServerThread(LiveServerThread):
    """LiveServerThread subclass that runs Daphne, as a singleton"""
    singleton = None

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.application = self.get_application()
        DaphneLiveServerThread.singleton = self

        # _pause is set by main thread to ask thread to pause
        self._pause = threading.Event()
        # _paused is set by thread to tell main thread it's done pausing
        self._paused = threading.Event()
        # _resume is set by main thread to ask thread to resume
        self._resume = threading.Event()
        # _resumed is set by thread to tell main thread it's done resuming
        self._resumed = threading.Event()

    def run(self):
        """
        Set up the live server and databases, and then loop over handling
        HTTP requests.
        """
        if self.connections_override:
            # Override this thread's database connections with the ones
            # provided by the main thread.
            for alias, conn in self.connections_override.items():
                connections[alias] = conn
        try:
            self.daphne = self._create_server()
            self.daphne.run()
        except Exception as e:
            self.error = e
            self.is_ready.set()
        finally:
            connections.close_all()

    def _create_server(self):
        """Create a daphne server with local thread asyncio event loop and twisted reactor"""
        # Reset reactor to use local thread's event loop
        from twisted.internet import asyncioreactor
        del sys.modules["twisted.internet.reactor"]

        try:
            event_loop = asyncio.get_event_loop()
        except RuntimeError:
            event_loop = asyncio.new_event_loop()

        asyncioreactor.install(event_loop)
        from twisted.internet import reactor

        # Create hook to check if main thread communicated with us
        reactor.callLater(1, self._on_reactor_hook, reactor)

        application = self.application
        if self.static_handler:
            application = self.static_handler(application)

        endpoints = build_endpoint_description_strings(host=self.host, port=self.port)

        def ready():
            if self.port == 0:
                self.port = self.daphne.listening_addresses[0][1]
            self.is_ready.set()

        return Server(
            application=application,
            endpoints=endpoints,
            signal_handlers=False,
            root_path=getattr(settings, "FORCE_SCRIPT_NAME", "") or "",
            ready_callable=ready,
            reactor=reactor,
        )

    def _on_reactor_hook(self, reactor):
        """Check for events from main thread, while within this thread"""
        if self._pause.is_set():
            self.do_pause()
            self._paused.set() # Notify main thread we cleared ok
            self._pause.clear() # Reset to allow pausing again
        if self._resume.is_set():
            self.do_resume()
            self._resumed.set() # Notify main thread we resumed ok
            self._resume.clear() # Reset to allow resuming again
        reactor.callLater(1, self._on_reactor_hook, reactor)

    def get_application(self):
        application = get_default_application()
        if self.static_handler:
            application = self.static_handler(application)

        return application

    def do_pause(self):
        """Pause daphne from thread"""
        self.application = None
        self.daphne.clear()

    def do_resume(self):
        """Resume daphne from thread"""
        # Override this thread's database connections with the ones
        # provided by the main thread.
        for alias, conn in self.connections_override.items():
            connections[alias] = conn
        self.application = self.get_application()
        self.daphne.set_application(self.application)

    def pause(self):
        """Pause daphne, from main thread"""
        self._pause.set() # Request thread to pause
        self._paused.wait() # Wait until thread has paused

    def resume(self, connections_override):
        """Resume daphne, with new connections_override, from main thread"""
        self.connections_override = connections_override
        self._resume.set() # Request thread to resume
        self._resumed.wait() # Wait until thread has resumed

    def terminate(self):
        """Stop thread, from main thread"""
        if hasattr(self, 'daphne'):
           # Stop the ASGI server
           self.daphne.stop()
        self.join()


class ChannelsLiveServerTestCase(LiveServerTestCase):
    """
    Drop-in replacement for Django's LiveServerTestCase.
    """
    server_thread_class = DaphneLiveServerThread
    static_handler = StaticFilesWrapper

    @classmethod
    def setUpClass(cls):
        """
        This is code is taken from Django's LiveServerTestCase, but uses the
        server thread as a singleton.
        """
        connections_override = {}
        for conn in connections.all():
            # If using in-memory sqlite databases, pass the connections to
            # the server thread.
            if conn.vendor == 'sqlite' and conn.is_in_memory_db():
                # Explicitly enable thread-shareability for this connection
                conn.allow_thread_sharing = True
                connections_override[conn.alias] = conn

        cls._live_server_modified_settings = modify_settings(
            ALLOWED_HOSTS={'append': cls.host},
        )
        cls._live_server_modified_settings.enable()
        if hasattr(cls.server_thread_class, 'singleton') and cls.server_thread_class.singleton:
            cls.server_thread = cls.server_thread_class.singleton
            cls.server_thread.resume(connections_override)
        else:
            cls.server_thread = cls._create_server_thread(connections_override)
            cls.server_thread.daemon = True
            cls.server_thread.start()

            # Wait for the live server to be ready
            cls.server_thread.is_ready.wait()
            if cls.server_thread.error:
                # Clean up behind ourselves, since tearDownClass won't get called in
                # case of errors.
                cls._tearDownClassInternal()
                raise cls.server_thread.error

    @classmethod
    def _tearDownClassInternal(cls):
        """
        This is code is taken from Django's LiveServerTestCase, but pauses the
        daphne server, instead of killing it.
        """
        # There may not be a 'server_thread' attribute if setUpClass() for some
        # reasons has raised an exception.
        if hasattr(cls, 'server_thread'):
            # Pause the live server's thread
            cls.server_thread.pause()

        # Restore sqlite in-memory database connections' non-shareability
        for conn in connections.all():
            if conn.vendor == 'sqlite' and conn.is_in_memory_db():
                conn.allow_thread_sharing = False
