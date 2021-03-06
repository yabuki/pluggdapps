# -*- coding: utf-8 -*-

# Derived work from Facebook's tornado server.

"""An I/O event loop for non-blocking sockets.

The server command is typically responsible for starting `HTTPIOLoop`,
    HTTPIOLoop( setting ).start()

In addition to I/O events, the `HTTPIOLoop` can also schedule time-based 
events.  `HTTPIOLoop.add_timeout` is a non-blocking alternative to 
`time.sleep`.
"""

import datetime, errno, heapq, time
import os, select, _thread, threading, traceback, signal

import pluggdapps.utils as h
import pluggdapps.utils.stack_context as sc

class HTTPIOLoop( object ):
    """A level-triggered I/O loop using Linux epoll and requires python 3."""
    # Constants from the epoll module
    _EPOLLIN    = 0x001
    _EPOLLPRI   = 0x002
    _EPOLLOUT   = 0x004
    _EPOLLERR   = 0x008
    _EPOLLHUP   = 0x010
    _EPOLLRDHUP = 0x2000
    _EPOLLONESHOT = (1 << 30)
    _EPOLLET    = (1 << 31)

    # Our events map exactly to the epoll events
    NONE  = 0
    READ  = _EPOLLIN
    WRITE = _EPOLLOUT
    ERROR = _EPOLLERR | _EPOLLHUP

    def __init__( self, sett ):
        self._evpoll = select.epoll()
        h.set_close_exec( self._evpoll.fileno() )
        self.poll_threshold = sett['poll_threshold']
        self.poll_timeout = sett['poll_timeout']

        # Book keeping
        self._handlers = {}
        """A map of polled descriptor and callback handlers."""
        self._events = {}
        self._callbacks = []
        self._callback_lock = threading.Lock()
        """Lock object to handle ioloop callbacks in a multi-threaded
        environment."""
        self._timeouts = []
        """A heap queue list to manage timeout events and its callbacks."""
        self._running = False
        """Initialized to True when start() is called and set to False to
        indicate that stop() is called."""
        self._stopped = False
        """Set to True when stop() is called and reset to False when start()
        exits."""
        self._thread_ident = None
        self._blocking_signal_threshold = None

        self._waker = Waker()
        """Create a pipe that we send bogus data to when we want to wake
        the I/O loop when it is idle."""

        # log.debug( "Adding poll-loop waker ..." )
        self.add_handler(
            self._waker.fileno(), lambda fd, events: self._waker.consume(),
            self.READ )


    #---- Manage polled descriptors and its callback handlers.

    def add_handler( self, fd, handler, events ):
        """Registers the given handler to receive the given events for fd."""
        self._handlers[fd] = sc.wrap(handler)
        self._evpoll.register( fd, events | self.ERROR )
        if len(self._handlers) > self.poll_threshold :
            #log.warning( 
            #  "Polled descriptors exceeded threshold %r", self.poll_threshold )
            pass

    def update_handler( self, fd, events ):
        """Changes the events we listen for fd."""
        self._evpoll.modify( fd, events | self.ERROR )

    def remove_handler( self, fd ):
        """Stop listening for events on fd."""
        self._handlers.pop(fd, None)
        self._events.pop(fd, None)
        try:
            self._evpoll.unregister(fd)
        except (OSError, IOError):
            #log.debug( "Error deleting fd from HTTPIOLoop", exc_info=True )
            pass


    #---- Manage timeout handlers on this epoll using heap queue.

    def add_timeout( self, deadline, callback ):
        """Calls the given callback at the time deadline from the I/O loop.

        Returns a handle that may be passed to remove_timeout to cancel.

        ``deadline`` may be a number denoting a unix timestamp (as returned
        by ``time.time()`` or a ``datetime.timedelta`` object for a deadline
        relative to the current time.

        Note that it is not safe to call `add_timeout` from other threads.
        Instead, you must use `add_callback` to transfer control to the
        HTTPIOLoop's thread, and then call `add_timeout` from there."""
        timeout = _Timeout( deadline, sc.wrap(callback) )
        heapq.heappush( self._timeouts, timeout )
        return timeout

    def remove_timeout( self, timeout ):
        """Cancels a pending timeout.

        The argument is a handle as returned by add_timeout.
        """
        # Removing from a heap is complicated, so just leave the defunct
        # timeout object in the queue (see discussion in
        # http://docs.python.org/library/heapq.html).
        # If this turns out to be a problem, we could add a garbage
        # collection pass whenever there are too many dead timeouts.
        timeout.callback = None


    #---- manage straight-forward callbacks inside evented ioloop.

    def add_callback( self, callback ):
        """Calls the given callback on the next I/O loop iteration.

        It is safe to call this method from any thread at any time.
        Note that this is the *only* method in HTTPIOLoop that makes this
        guarantee; all other interaction with the HTTPIOLoop must be done
        from that HTTPIOLoop's thread.  add_callback() may be used to transfer
        control from other threads to the HTTPIOLoop's thread.
        """
        with self._callback_lock :
            list_empty = not self._callbacks
            self._callbacks.append(sc.wrap(callback))

        if list_empty and _thread.get_ident() != self._thread_ident:
            # If we're in the HTTPIOLoop's thread, we know it's not currently
            # polling.  If we're not, and we added the first callback to an
            # empty list, we may need to wake it up (it may wake up on its
            # own, but an occasional extra wake is harmless).  Waking
            # up a polling HTTPIOLoop is relatively expensive, so we try to
            # avoid it when we can.
            self._waker.wake()

    def _run_callback( self, callback ):
        try:
            callback()
        except Exception:
            self.handle_callback_exception( callback )

    def handle_callback_exception(self, callback):
        """This method is called whenever a callback run by the HTTPIOLoop
        throws an exception.

        By default simply logs the exception as an error.  Subclasses
        may override this method to customize reporting of exceptions.

        The exception itself is not passed explicitly, but is available
        in sys.exc_info.
        """
        #log.error( "Exception in callback %r", callback, exc_info=True )
        pass

    #---- Shutdown methods

    def close( self, all_fds=False ):
        """Closes the HTTPIOLoop, freeing any resources used.

        If ``all_fds`` is true, all file descriptors registered on the
        HTTPIOLoop will be closed (not just the ones created by the HTTPIOLoop
        itself).

        Typically only one HTTPIOLoop will be active for the entire lifetime
        of the process. In that case closing the HTTPIOLoop is not necessary
        since everything will be cleaned up when the process exits.
        `HTTPIOLoop.close` is provided mainly for scenarios such as unit
        tests, which create and destroy a large number of IOLoops.

        An HTTPIOLoop must be completely stopped before it can be closed.  This
        means that `HTTPIOLoop.stop()` must be called *and* 
        `HTTPIOLoop.start()` must be allowed to return before attempting to 
        call `HTTPIOLoop.close()`. Therefore the call to `close` will usually 
        appear just after the call to `start` rather than near the call to 
        `stop`. """
        self.remove_handler(self._waker.fileno())
        if all_fds:
            for fd in self._handlers.keys() :
                try:
                    os.close(fd)
                except Exception:
                    #log.debug( "error closing fd %s", fd, exc_info=True )
                    pass
        self._waker.close()
        self._evpoll.close()

    def stop( self ):
        """Stop the loop after the current event loop iteration is complete.
        If the event loop is not currently running, the next call to start()
        will return immediately.

        To use asynchronous methods from otherwise-synchronous code (such as
        unit tests), you can start and stop the event loop like this::

          ioloop = HTTPIOLoop()
          async_method(ioloop=ioloop, callback=ioloop.stop)
          ioloop.start()

        ioloop.start() will return after async_method has run its callback,
        whether that callback was invoked before or after ioloop.start.

        Note that even after `stop` has been called, the HTTPIOLoop is not
        completely stopped until `HTTPIOLoop.start` has also returned.
        """
        #log.debug("Stopping poll loop ...")
        self._running = False
        self._stopped = True
        self._waker.wake()  # Wake the ioloop

    def running( self ):
        """Returns true if this HTTPIOLoop is currently running."""
        return self._running


    #---- Manager idle poll using a signal threshold.

    def set_blocking_signal_threshold(self, seconds, action):
        """Sends a signal if the ioloop is blocked for more than s seconds.

        Pass seconds=None to disable.  Requires python 2.6 on a unixy
        platform.

        The action parameter is a python signal handler.  Read the
        documentation for the python 'signal' module for more information.
        If action is None, the process will be killed if it is blocked for
        too long."""
        if not hasattr( signal, "setitimer"):
            #log.error( "set_blocking_signal_threshold requires a signal module "
            #            "with the setitimer method"  )
            return
        self._blocking_signal_threshold = seconds
        if seconds is not None:
            signal.signal(
                signal.SIGALRM,
                action if action is not None else signal.SIG_DFL)

    def set_blocking_log_threshold(self, seconds):
        """Logs a stack trace if the ioloop is blocked for more than s seconds.
        Equivalent to set_blocking_signal_threshold(seconds, self.log_stack).
        """
        self.set_blocking_signal_threshold(seconds, self.log_stack)

    def log_stack(self, signal, frame):
        """Signal handler to log the stack trace of the current thread.

        For use with set_blocking_signal_threshold."""
        #log.warning( 'HTTPIOLoop blocked for %f seconds in\n%s',
        #              self._blocking_signal_threshold, 
        #             ''.join(traceback.format_stack(frame)) )
        pass


    #---- Perform evented polling.

    def start( self ):
        """Starts the I/O loop.

        The loop will run until one of the I/O handlers calls stop(), which
        will make the loop stop after the current event iteration completes.
        """
        if self._stopped:
            self._stopped = False
            return
        self._thread_ident = _thread.get_ident()
        self._running = True
        while True:
            poll_timeout = self.poll_timeout

            # Prevent IO event starvation by delaying new callbacks
            # to the next iteration of the event loop.
            with self._callback_lock:
                callbacks = self._callbacks
                self._callbacks = []

            [ self._run_callback(callback) for callback in callbacks ]

            if self._timeouts :
                now = time.time()
                while self._timeouts:
                    if self._timeouts[0].callback is None:
                        # the timeout was cancelled
                        heapq.heappop(self._timeouts)
                    elif self._timeouts[0].deadline <= now:
                        timeout = heapq.heappop(self._timeouts)
                        self._run_callback(timeout.callback)
                    else:
                        seconds = self._timeouts[0].deadline - now
                        poll_timeout = min(seconds, poll_timeout)
                        break

            if self._callbacks:
                # If any callbacks or timeouts called add_callback,
                # we don't want to wait in poll() before we run them.
                poll_timeout = 0.0

            if self._running == False : # stop() is called !
                break

            if self._blocking_signal_threshold is not None :
                # clear alarm so it doesn't fire while poll is waiting for
                # events.
                signal.setitimer( signal.ITIMER_REAL, 0, 0 )

            try:
                event_pairs = self._evpoll.poll(poll_timeout)
            except Exception as e:
                # Depending on python version and HTTPIOLoop implementation,
                # different exception types may be thrown and there are
                # two ways EINTR might be signaled:
                # * e.errno == errno.EINTR
                # * e.args is like (errno.EINTR, 'Interrupted system call')
                if (getattr(e, 'errno', None) == errno.EINTR or
                    (isinstance(getattr(e, 'args', None), tuple) and
                     len(e.args) == 2 and e.args[0] == errno.EINTR)):
                    continue
                else:
                    raise

            if self._blocking_signal_threshold is not None:
                signal.setitimer( signal.ITIMER_REAL,
                                  self._blocking_signal_threshold, 0 )

            # Pop one fd at a time from the set of pending fds and run
            # its handler. Since that handler may perform actions on
            # other file descriptors, there may be reentrant calls to
            # this HTTPIOLoop that update self._events
            self._events.update(event_pairs)
            while self._events:
                fd, events = self._events.popitem()
                try:
                    self._handlers[fd](fd, events)
                except (OSError, IOError) as e:
                    if e.args[0] == errno.EPIPE:
                        # Happens when the client closes the connection
                        pass
                    else:
                        #log.error( "Exception in I/O handler for fd %s",
                        #           fd, exc_info=True )
                        pass
                except Exception:
                    #log.error( "Exception in I/O handler for fd %s",
                    #           fd, exc_info=True )
                    pass
        # reset the stopped flag so another start/stop pair can be issued
        self._stopped = False
        if self._blocking_signal_threshold is not None:
            signal.setitimer( signal.ITIMER_REAL, 0, 0 )

class _Timeout( object ):
    """An HTTPIOLoop timeout, a UNIX timestamp and a callback"""

    # Reduce memory overhead when there are lots of pending callbacks
    __slots__ = ['deadline', 'callback']

    def __init__( self, deadline, callback ):
        if isinstance(deadline, (int, float)):
            self.deadline = deadline
        elif isinstance(deadline, datetime.timedelta):
            self.deadline = time.time() + h.timedelta_to_seconds(deadline)
        else:
            raise TypeError("Unsupported deadline %r" % deadline)
        self.callback = callback

    # Comparison methods to sort by deadline, with object id as a tiebreaker
    # to guarantee a consistent ordering.  The heapq module uses __le__
    # in python2.5, and __lt__ in 2.6+ (sort() and most other comparisons
    # use __lt__).
    def __lt__( self, other ):
        return ((self.deadline, id(self)) <
                (other.deadline, id(other)))

    def __le__( self, other ):
        return ((self.deadline, id(self)) <=
                (other.deadline, id(other)))


class PeriodicCallback( object ):
    """Schedules the given callback to be called periodically.

    The callback is called every callback_time milliseconds.

    `start` must be called after the PeriodicCallback is created.
    """
    def __init__( self, callback, callback_time, ioloop ):
        self.callback = callback
        self.callback_time = callback_time
        self.ioloop = ioloop
        self._running = False
        self._timeout = None

    def start( self ):
        """Starts the timer."""
        self._running = True
        self._next_timeout = time.time()
        self._schedule_next()

    def stop( self ):
        """Stops the timer."""
        self._running = False
        if self._timeout is not None:
            self.ioloop.remove_timeout(self._timeout)
            self._timeout = None

    def _run( self ):
        if not self._running:
            return
        try:
            self.callback()
        except Exception:
            #log.error("Error in periodic callback", exc_info=True)
            pass
        self._schedule_next()

    def _schedule_next( self ):
        if self._running:
            current_time = time.time()
            while self._next_timeout <= current_time:
                self._next_timeout += self.callback_time / 1000.0
            self._timeout = \
                    self.ioloop.add_timeout( self._next_timeout, self._run )

class Waker( object ):
    def __init__(self):
        r, w = os.pipe()
        h.set_nonblocking(r, w)
        h.set_close_exec(r, w)
        self.reader = os.fdopen(r, "rb", 0)
        self.writer = os.fdopen(w, "wb", 0)

    def fileno(self):
        return self.reader.fileno()

    def wake(self):
        try           : self.writer.write("x")
        except IOError: pass

    def consume(self):
        try :
            ss, s = b'', self.reader.read()
            while s :
                ss += s 
                s = self.reader.read()
        except IOError:
            pass
        return ss

    def close(self):
        self.reader.close()
        self.writer.close()

