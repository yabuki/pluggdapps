# -*- coding: utf-8 -*-

"""HTTP web server based on epoll event-loop using non-blocking sockets.

In addition to I/O events, the server also does generic callback handling and
schedule time-based events.
"""

import sys, datetime, errno, heapq, time, os, select, socket, re, \
       collections, http.client

import ssl  # Python 2.6+

import pluggdapps.utils             as h
from   pluggdapps.plugin            import Plugin, implements
from   pluggdapps.web.webinterfaces import IHTTPRequest
from   pluggdapps.interfaces        import IHTTPServer, IHTTPConnection


# TODO :
#   * All Internet-based HTTP/1.1 servers MUST respond with a 400 (Bad
#     Request) status code to any HTTP/1.1 request message which lacks a Host
#     header field.

_ds1 = h.ConfigDict()
_ds1.__doc__ = \
    "Configuration settings for event poll based HTTP server."

_ds1['scheme'] = {
    'default'  : 'http',
    'types'    : (str,),
    'help'     : "HTTP Scheme to use, either `http` or `https`. If left "
                 "empty `scheme` parameter from [pluggdapps] section will be "
                 "used."
}
_ds1['host']  = {
    'default' : 'localhost',
    'types'   : (str,),
    'help'    : "Address may be either an IP address or hostname.  If it's a "
                "hostname, the server will listen on all IP addresses "
                "associated with the name. Address may be an empty string "
                "or None to listen on all available interfaces. Family may "
                "be set to either ``socket.AF_INET`` or ``socket.AF_INET6`` "
                "to restrict to ipv4 or ipv6 addresses, otherwise both will "
                "be used if available. If left empty `host` parameter from "
                "[pluggdapps] section will be used.",

}
_ds1['port']  = {
    'default' : 0,
    'types'   : (int,),
    'help'    : "Port addres to bind the http server. If left empty `port` "
                "paramter from [pluggdapps] section will be used."
}
_ds1['family'] = {
    'default' : 'AF_INET',
    'types'   : (str,),
    'help'    : "Family may be set to either ``AF_INET`` or ``AF_INET6`` "
                "to restrict to ipv4 or ipv6 addresses, otherwise both will "
                "be used if available.",
}
_ds1['backlog']  = {
    'default' : 128,
    'types'   : (int,),
    'help'    : "Back log of http request that can be queued at listening "
                "port. This option is directly passed to socket.listen()."
}
_ds1['xheaders']  = {
    'default' : False,
    'types'   : (bool,),
    'help'    : "If `True`, `X-Real-Ip`` and `X-Scheme` headers are "
                "supported, will override the remote IP and HTTP scheme for "
                "all requests. These headers are useful when running "
                "pluggdapps behind a reverse proxy or load balancer.",
}
_ds1['IHTTPConnection']  = {
    'default' : 'httpconnection',
    'types'   : (str,),
    'help'    : "Plugin to handle client connections."
}

#---- SSL settings, for scheme `https`
_ds1['ssl.certfile']  = {
    'default' : '',
    'types'   : (str,),
    'help'    : "SSL Certificate file location.",

}
_ds1['ssl.keyfile']   = {
    'default' : '',
    'types'   : (str,),
    'help'    : "SSL Key file location.",

}
_ds1['ssl.cert_reqs']  = {
    'default' : ssl.CERT_REQUIRED,
    'types'   : (int,),
    'options' : [ ssl.CERT_NONE, ssl.CERT_OPTIONAL, ssl.CERT_REQUIRED ],
    'help'    : "Whether a certificate is required from the other side of "
                "the connection, and whether it will be validated if "
                "provided. It must be one of the three values CERT_NONE "
                "(certificates ignored), CERT_OPTIONAL (not required, but "
                "validated if provided), or CERT_REQUIRED (required and "
                "validated). If the value of this value is not CERT_NONE, "
                "then the `ca_certs` parameter must point to a file of CA "
                "certificates."
}
_ds1['ssl.ca_certs']   = {
    'default' : None,
    'types'   : (str,),
    'help'    : "The ca_certs file contains a set of concatenated "
                "certification authority. certificates, which are used to "
                "validate certificates passed from the other end of the "
                "connection."
}
#---- Setting for HTTPIOLoop
_ds1['poll_threshold']     = {
    'default' : 1000,
    'types'   : (int,),
    'help'    : "A warning limit for number of descriptors being polled by a "
                "single poll instance. Will be used by HTTPIOLoop definition",
}
_ds1['poll_timeout']       = {
    'default' : 3600.0,
    'types'   : (float,),
    'help'    : "In seconds. Poll instance will timeout after so many "
                "seconds and perform callbacks (if any) and start a fresh "
                "poll. Will be used by HTTPIOLoop definition",
}

def run_callback( server, callback, *args, **kwargs ):
    """Run a callback with `args`."""
    try:
        callback( *args, **kwargs )
    except Exception:
        server.pa.logerror( "Exception in callback %r" % callback )
        if server['debug'] : raise

class HTTPEPollServer( Plugin ):
    """A non-blocking, single-threaded HTTP Server plugin.

    `HTTPEPollServer` can serve SSL traffic with Python 2.6+ and OpenSSL. 
    To make this server serve SSL traffic, configure this plugin with
    `ssl.*` settings which is required for the `ssl.wrap_socket`
    method, including "certfile" and "keyfile".

    Server resolves application for HTTP requests and dispatches them to
    corresponding :class:`IApplication` plugin. Finishing the request does
    not necessarily close the connection in the case of HTTP/1.1 keep-alive
    requests.

    Server features :

    * Basic connection handler parsing request startline, headers and body.
    * If ``xheaders`` is ``True``, we support the ``X-Real-Ip`` and
      ``X-Scheme`` headers, which override the remote IP and HTTP scheme for
      all requests.  These headers are useful when running pluggdapps behind a
      reverse proxy or load balancer. 
    """

    implements( IHTTPServer )

    ioloop = None
    "IOLoop instance for event-polling."

    def __init__( self ):

        self.version = b'HTTP/1.1'

        # Note `ioloop` can be started/stopped/closed only by the server plugin
        # that instantiates IOLoop()
        self.ioloop = IOLoop( self )

        # Attributes
        self.sockets = {}      # fd->socket mapping for listening sockets.
        self.connections = []  # [ HTTPConnection() ]

    #---- IHTTPServer interface methods.

    def start( self ):
        """Starts this server using IOloop (EPoll)."""
        self.listen()
        try :
            self.ioloop.start() # Block !
        except :
            typ, val, tb = sys.exc_info()
            h.print_exc( typ, val, tb )
            self.stop()
            if self['debug'] : raise
        self.ioloop.close()

    def stop( self ):
        """Stop listening for new connections. Expected to be called in case
        of exceptions and SIGNALS"""
        # Stop EPoll, this must un-block ioloop.start() call. Do close() after
        # that.
        self.ioloop.stop()
        # Close all connections.
        [ httpconn.close() for httpconn in self.connections ]
        # Close listening sockets
        [ sock.close() for sock in self.sockets.values() ]

    #---- Internal methods

    def close_connection( self, httpconn ):
        """Close a client connection `httpconn` which is
        :class:`IHTTPConnection` plugin."""
        if httpconn in self.connections :
            self.connections.remove( httpconn )

    def listen( self ):
        """Starts accepting connections on the given port.

        This method may be called more than once to listen on multiple ports.
        `listen` takes effect immediately; it is not necessary to call
        `HTTPEPollServer.start` afterwards.  It is, however, necessary to 
        start the `IOLoop`.
        """
        sockets = self.bind_sockets()
        self.add_sockets( sockets )

    def add_sockets( self, sockets ):
        """Make the server start accepting connections using event loop on the
        given sockets.

        The ``sockets`` parameter is a list of socket objects such as
        those returned by `bind_sockets`.
        """
        for sock in sockets:
            self.sockets[ sock.fileno() ] = sock
            add_accept_handler(self, sock, self.handle_connection, self.ioloop)

    def handle_connection( self, conn, address ):
        httpconn = None     # if query_plugin bombs.
        try :
            httpconn = self.query_plugin( IHTTPConnection, 
                                          self['IHTTPConnection'],
                                          conn, address, self )
            self.connections.append( httpconn )

        except ssl.SSLError as err:
            self.pa.logerror( "Error(%s) in SSL connection %r" % (
                              address, err.args[0] )
                            )
            httpconn.close() if httpconn else None
            if self['debug'] : raise

        except socket.error as err:
            self.pa.logerror( "Socket Error(%s) in connection %r" % (
                              address, err.args[0] )
                            )
            httpconn.close() if httpconn else None
            if self['debug'] : raise

        except Exception:
            self.pa.logerror( "Error in accepting connection ... " )
            httpconn.close() if httpconn else None
            if self['debug'] : raise

    def bind_sockets( self ):
        """Creates listening sockets (server) bound to the given port and 
        address.

        Returns a list of socket objects (multiple sockets are returned if
        the given address maps to multiple IP addresses, which is most common
        for mixed IPv4 and IPv6 use).

        Address may be either an IP address or hostname.  If it's a hostname,
        the server will listen on all IP addresses associated with the
        name.  Address may be an empty string or None to listen on all
        available interfaces.  Family may be set to either socket.AF_INET
        or socket.AF_INET6 to restrict to ipv4 or ipv6 addresses, otherwise
        both will be used if available.

        The ``backlog`` argument has the same meaning as for 
        ``socket.listen()``.
        """
        family = { 'AF_UNSPEC' : socket.AF_UNSPEC,
                   'AF_INET6'  : socket.AF_INET6,
                   'AF_INET'   : socket.AF_INET
                 }.get( self['family'], None )
        family = family or socket.AF_UNSPEC
        address = self['host'] or self.pa.settings['pluggdapps']['host']
        scheme = self['scheme'] or self.pa.settings['pluggdapps']['scheme']
        port = self['port'] or self.pa.settings['pluggdapps']['port']
        port = port or h.port_for_scheme( scheme )
        backlog = self['backlog']

        sockets = []
        address = None if address == "" else address
        flags = socket.AI_PASSIVE
        addrinfo = set([])

        if hasattr(socket, "AI_ADDRCONFIG"):
            # AI_ADDRCONFIG ensures that we only try to bind on ipv6
            # if the system is configured for it, but the flag doesn't
            # exist on some platforms (specifically WinXP, although
            # newer versions of windows have it)
            flags |= socket.AI_ADDRCONFIG
            addrinfo = set(
                socket.getaddrinfo(
                    address, port, family, socket.SOCK_STREAM, 0, flags))

        for res in addrinfo :
            self.pa.loginfo( "Binding socket for %s ..." % (res,) )
            af, socktype, proto, canonname, sockaddr = res
            sock = socket.socket(af, socktype, proto)
            h.set_close_exec( sock.fileno() )
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if af == socket.AF_INET6:
                # On linux, ipv6 sockets accept ipv4 too by default,
                # but this makes it impossible to bind to both
                # 0.0.0.0 in ipv4 and :: in ipv6.  On other systems,
                # separate sockets *must* be used to listen for both ipv4
                # and ipv6.  For consistency, always disable ipv4 on our
                # ipv6 sockets and use a separate ipv4 socket when needed.
                #
                # Python 2.x on windows doesn't have IPPROTO_IPV6.
                if hasattr(socket, "IPPROTO_IPV6"):
                    sock.setsockopt( 
                            socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1 )
            self.pa.loginfo( "Set server socket to non-blocking mode ..." )
            sock.setblocking(0) # Set to non-blocking.
            sock.bind( sockaddr )
            self.pa.loginfo( 
                    "Server listening with a backlog of %s" % backlog )
            sock.listen( backlog )
            sockets.append( sock )
        return sockets

    #---- ISettings interface methods

    @classmethod
    def default_settings( cls ):
        return _ds1

    @classmethod
    def normalize_settings( cls, sett ):
        sett['port']  = h.asint( sett['port'], _ds1['port'] )
        sett['backlog'] = h.asint( sett['backlog'], _ds1['backlog'] )
        sett['xheaders'] = h.asbool( sett['xheaders'], _ds1['xheaders'] )
        sett['ssl.cert_reqs'] = \
                h.asint( sett['ssl.cert_reqs'], _ds1['ssl.cert_reqs'] )
        sett['poll_threshold'] = \
                h.asint( sett['poll_threshold'], _ds1['poll_threshold'] )
        sett['poll_timeout'] = \
                h.asfloat( sett['poll_timeout'], _ds1['poll_timeout'] )
        return sett


def add_accept_handler( server, sock, callback, ioloop ):
    """Adds an ``IOLoop`` event handler to accept new connections on 
    ``sock``.

    When a connection is accepted, ``callback(connection, address)`` will
    be run (``connection`` is a socket object, and ``address`` is the
    address of the other end of the connection).  Note that this signature
    is different from the ``callback(fd, events)`` signature used for
    ``IOLoop`` handlers.
    """
    def accept_handler( fd, events ):
        while True:
            try :
                connection, address = sock.accept()
            except socket.error as e:
                if e.args[0] in (errno.EWOULDBLOCK, errno.EAGAIN):
                    return
                server.pa.loginfo( "Unable to accept connection." )
                if self['debug'] : raise

            server.pa.loginfo( "Accepting new connection from %r"%(address,) )
            callback( connection, address )

    ioloop.add_handler( sock.fileno(), accept_handler, IOLoop.READ )


class IOLoop( object ):
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

    # Book keeping
    _evpoll = None
    """EPoll descriptor, returned by select.epoll()."""

    _handlers = {}
    """A map of polled descriptor and callback handlers."""

    _events = {}
    """Dictionary of file-descriptors and events that woke-up the
    descriptor."""

    _callbacks = []
    """Straight forward callbacks."""

    _timeouts = []
    """A heap queue list to manage timeout events and its callbacks."""

    _running = False
    """Initialized to True when start() is called and set to False to
    indicate that stop() is called."""

    _stopped = False
    """Set to True when stop() is called and reset to False when start()
    exits."""

    _waker = None
    """Create a pipe that we send bogus data to when we want to wake
    the I/O loop when it is idle."""

    poll_threshold = None
    """Maximum number of descriptors to poll on epoll()."""

    poll_timeout = None
    """Timout value while waiting on epoll()."""

    server = None
    """:class:`IHTTPServer` plugin."""

    def __init__( self, server ):

        self.poll_threshold = server['poll_threshold']
        self.poll_timeout = server['poll_timeout']
        self.server = server
        self.debug = server['debug']

        self._evpoll = select.epoll()
        self._waker = Waker()

        h.set_close_exec( self._evpoll.fileno() )

        # Book keeping
        self._handlers = {}
        self._events = {}
        self._callbacks = []
        self._timeouts = []
        self._running = False
        self._stopped = False

        if self.debug :
            server.pa.loginfo( "Adding poll-loop waker ..." )

        self.add_handler( self._waker.fileno(), 
                          lambda fd, events: self._waker.consume(), # handler
                          self.READ )

    #---- Manage polled descriptors and its callback handlers.

    def add_handler( self, fd, callback, events ):
        """Registers the given `callback` to receive the given events for fd.

        Note that exceptions by this socket `callback` must be handled within
        the callback itself.
        """
        self._handlers[fd] = callback
        self._evpoll.register( fd, events | self.ERROR )
        if len(self._handlers) > self.poll_threshold :
            self.server.pa.logwarn(
                "Polled descriptors exceeded threshold" % self.poll_threshold
            )
        if self.debug :
            self.server.pa.loginfo(
                "Add descriptor to epoll : %s" % list( self._handlers.keys() )
            )

    def update_handler( self, fd, events ):
        """Changes the events we listen for fd."""
        self._evpoll.modify( fd, events | self.ERROR )
        if self.debug :
            self.server.pa.loginfo(
                "Updating descriptor : (%s, %s)" % (fd, events)
            )

    def remove_handler( self, fd ):
        """Stop listening for events on fd."""
        if self.debug :
            self.server.pa.loginfo("Remove descriptor from epoll : %s:" % fd)

        self._handlers.pop(fd, None)
        self._events.pop(fd, None)
        try:
            self._evpoll.unregister(fd)
        except (OSError, IOError):
            self.server.pa.logwarn( "Error deleting fd from epoll" )

    #---- Manage timeout handlers on this epoll using heap queue.

    def add_timeout( self, deadline, callback ):
        """Calls the given callback at the time deadline from IOloop.

        Returns a handle that may be passed to remove_timeout to cancel.

        ``deadline`` may be a number denoting a unix timestamp (as returned
        by ``time.time()`` or a ``datetime.timedelta`` object for a deadline
        relative to the current time.
        
        Note that exceptions by timeout `callback` must be handled within the
        callback itself.
        """
        timeout = Timeout( deadline, callback )
        heapq.heappush( self._timeouts, timeout )
        return timeout

    def remove_timeout( self, timeout ):
        """Cancels a pending timeout. The argument is a handle as returned by
        add_timeout().
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
        
        Note that exceptions within the `callback` must handled within the
        callback itself.
        """
        list_empty = self._callbacks
        self._callbacks.append( callback )
        self._waker.wake() if list_empty else None

    #---- Perform evented polling.

    def start( self ):
        """Starts the I/O loop.

        The loop will run until one of the I/O handlers calls stop(), which
        will make the loop stop after the current event iteration completes.
        """
        if self._stopped :
            self._stopped = False
            return

        self._running = True
        while True :
            poll_timeout = self.poll_timeout

            # Prevent IO event starvation by delaying new callbacks
            # to the next iteration of the event loop.
            callbacks = self._callbacks
            self._callbacks = []
            for callback in callbacks :
                try    : callback()
                except :
                    err = "Exception in callback %r" % callback
                    self.server.pa.logerror( err )
                    if self.debug : raise

            # Handle timeouts
            if self._timeouts :
                now = time.time()
                while self._timeouts :
                    if self._timeouts[0].callback is None : # Cancelled timeout
                        heapq.heappop( self._timeouts )

                    elif self._timeouts[0].deadline <= now : # Handle timeout
                        timeout = heapq.heappop( self._timeouts )
                        try    : timeout.callback()
                        except :
                            err = "Exception in callback %r" % timeout
                            self.server.pa.logerror( err )
                            if self.debug : raise

                    else : # Adjust poll-timeout
                        seconds = self._timeouts[0].deadline - now
                        poll_timeout = min(seconds, poll_timeout)
                        break

            if self._callbacks :
                # If any callbacks or timeouts called add_callback,
                # we don't want to wait in poll() before we run them.
                poll_timeout = 0.0

            if self._running == False : # stop() is called !
                break

            try:
                event_pairs = self._evpoll.poll( poll_timeout )
            except Exception as e:
                # Depending on python version and event-loop implementation,
                # different exception types may be thrown and there are
                # two ways EINTR might be signaled:
                # * e.errno == errno.EINTR
                # * e.args is like (errno.EINTR, 'Interrupted system call')
                if getattr(e, 'errno', None) == errno.EINTR : continue
                if e.args[0] == errno.EINTR : continue 

                raise # This exception is bad ! forcefully raise exception

            # Pop one fd at a time from the set of pending fds and run
            # its handler. Since that handler may perform actions on
            # other file descriptors, there may be reentrant calls to
            # this IOLoop that update self._events
            self._events.update(event_pairs)
            while self._events :
                fd, events = self._events.popitem()
                callback = self._handlers.get( fd, None )
                try : callback( fd, events ) if callback else None
                except :
                    err = "Exception in socket callback %r" % callback
                    self.server.pa.logerror( err )
                    if self.server['debug'] : raise

        # reset the stopped flag so another start/stop pair can be issued
        self._stopped = False

    #---- Shutdown methods

    def stop( self ):
        """Stop the loop after the current event loop iteration is complete.
        If the event loop is not currently running, the next call to start()
        will return immediately.

        Note that even after `stop` has been called, epoll is not completely
        stopped until `self.start()` has also returned.
        """
        # Close listening sockets and remove them from IOLoop
        self.server.pa.loginfo( "Stopping poll loop ..." )
        fds = list( self._handlers.keys() )
        [ self.remove_handler( fd ) for fd in fds[:] ]
        self._running = False
        self._stopped = True
        self._waker.wake()  # Wake the ioloop

    def close( self ):
        """Closes the event-poll, freeing any resources used.

        An IOLoop must be completely stopped before it can be closed.  This
        means that `self.stop()` must be called *and* 
        `self.start()` must be allowed to return before attempting to 
        call `IOLoop.close()`. Therefore the call to `close` will usually 
        appear just after the call to `start` rather than near the call to 
        `stop`. """
        self.remove_handler( self._waker.fileno() )
        self._waker.close()
        self._evpoll.close()



_ds2 = h.ConfigDict()
_ds2.__doc__ = \
    "Configuration settings for event poll based HTTP server."

_ds2['no_keep_alive']  = {
    'default' : False,
    'types'   : (bool,),
    'help'    : "HTTP /1.1, whether to close the connection after every "
                "request.",
}
_ds2['connection_timeout']  = {
    'default' : 60*60*1,    # 1 hours
    'types'   : (int,),
    'help'    : "Timeout after which an idle connection is gracefully closed."
}
_ds2['max_buffer_size'] = {
    'default' : 104857600,  # 100MB
    'types'   : (int,),
    'help'    : "Maximum size of read / write buffer."
}
_ds2['read_chunk_size'] = {
    'default' : 4096,
    'types'   : (int,),
    'help'    : "Chunk of data to read at a time."
}


class HTTPConnection( Plugin ):
    """Handles a connection to an HTTP client, executing HTTP requests.

    We parse HTTP headers and bodies, and execute the request callback
    until the HTTP conection is closed.

    * Accepts only HTTP/1.1 request. If otherwise, reponds with bad-request
      (400) and closes the connection.
    """

    implements( IHTTPConnection )

    write_callback = None
    """Call-back for writing data to connection."""

    close_callback = None
    """Call-back when connection is closed."""

    finish_callback = None
    """Call-back when request is finished."""

    stream = None
    """:class:`IOStream` object."""
    reqdata = None
    """Tuple of new request's start line and headers,
    (method, uri, version, hdrs)"""

    chunk = None
    """Tuple of on-going request chunk, (chunk_size, chunk_ext, chunk_data)"""

    # error response
    BAD_REQUEST    = ( b'HTTP/1.1 400 ' + 
                       http.client.responses[400].encode('utf8') + 
                       b'\r\n\r\n' )
    NOT_FOUND      = ( b'HTTP/1.1 404 ' + 
                       http.client.responses[404].encode('utf8') + 
                       b'\r\n\r\n' )
    ENTITY_LARGE   = ( b'HTTP/1.1 413 ' + 
                       http.client.responses[413].encode('utf8') +
                       b'\r\n\r\n' )
    INTERNAL_ERROR = ( b'HTTP/1.1 500 ' + 
                       http.client.responses[500].encode('utf8') + 
                       b'\r\n\r\n' )

    def __init__( self, conn, addr, server ):
        from pluggdapps import __version__

        self.conn = conn
        self.address = addr
        self.server = server
        self.product = b'PluggdappsServer/' + __version__.encode('utf8')
        self.version = server.version
        self.request = None

        # Book-keeping
        self.ioloop = server.ioloop
        self.stream = None

        # Book-keeping for on-going request
        self.write_callback = None
        self.close_callback = None
        self.finish_callback = None
        self.reqdata = None

        # Set up a socket from accepted connection (conn, addr).
        sslopts = h.settingsfor( 'ssl.', server )
        scheme = server['scheme'] or self.pa.settings['pluggdapps']['scheme']
        if scheme == 'https' :
            self.conn = ssl.wrap_socket( conn, server_side=True,
                                         do_handshake_on_connect=False,
                                         **sslopts )
        streamcls = SSLIOStream if scheme == 'https' else IOStream
        self.stream = streamcls( self )

        # Poll for request start-line
        self.stream.read_until( b"\r\n\r\n", self.on_request_headers )
        self.stream.set_close_callback( self.on_connection_close )

        # Subscribe timeout
        self.ioloop.add_timeout( self['connection_timeout'], self.on_timeout )

    def get_ssl_certificate( self ):
        """Return SSL certificate for SSL traffic."""
        try : return self.conn.get_ssl_certificate()
        except ssl.SSLError : return None

    def set_close_callback( self, callback ):
        self.close_callback = callback

    def set_finish_callback( self, callback ):
        self.finish_callback = callback

    def handle_request( 
            self, method, uri, version, headers, body=None, chunk=None,
            trailers=None ):

        # Fresh request, resolve application.
        try :
            uriparts, mountedat = self.pa.resolveapp( uri, headers )
            if mountedat :
                webapp = mountedat[2]
            else :
                self.pa.logerror(
                    "Unable to resolve request for apps. (%s)" % uri )
                return
        except :
            self.write( self.NOT_FOUND, force=True )
            self.tryclose( disconnect=True )
            if self['debug'] : raise

        try :
            # Since the connection plugin do not operate in the context
            # of a webapp, use `webapp` plugin to query for IHTTPRequest.
            request = webapp.query_plugin(
                            IHTTPRequest, webapp['IHTTPRequest'],
                            self, method, uriparts, version, headers )
        except :
            self.write( self.NOT_FOUND, force=True )
            self.tryclose( disconnect=True )
            self.pa.logerror(
                "Application %r cannot handle request %r" % (webapp, uri) )
            if self['debug'] : raise

        self.request = request
        if chunk :
            handle_chunk( chunk=chunk, trailers=trailers )
        else :
            webapp.dorequest( request, body=body )

    def handle_chunk( self, chunk, trailers=None ):
        self.request.webapp.dorequest(request, chunk=chunk, trailers=trailers)

    def write( self, chunk, callback=None, force=False ):
        """Write a chunk of data."""
        response = getattr( self.request, 'response', None )
        if force == False and self.request == None :
            self.pa.logerror( "Request is not yet received." )
            return

        if force == False and response == None :
            self.pa.logerror(
                    "Writing on connection without a response object" ) 
            return

        if self.stream.closed() :
            self.pa.logwarn( 
                    "Cannot write to closed stream %r" % (self.address,) )
            return

        if response and response.ischunked() :
            chunk_size = len( chunk )
            data = ( hex(chunk_size).encode('utf8') + b'\r\n' 
                     + chunk + b'\r\n' )
        else :
            data = chunk

        self.write_callback = callback
        self.stream.write( data, self.on_write_complete )

        return

    def finish( self, callback=None ):
        if self.request == None :
            self.pa.logerror( "Cannot finish for already finished request." )
            return

        self.finish_callback = callback

        # If write is pending, leave finish to write-callback
        if not self.stream.writing() :
            self.dofinish()

    def close( self ):
        """Close the connection with client."""
        self.tryclose( disconnect=True )

    #---- Internal methods

    def supports_http_1_1( self ):
        """Check whether the client support HTTP 1.1"""
        if self.reqdata :
            return self.reqdata[2] == b'HTTP/1.1'
        return False

    def dofinish( self ):
        """Mark that response is sent and close the connection if required,
        before subscribing to request-handler."""
        disconnect = self.tryclose()

        if self.finish_callback :
            callback, self.finish_callback = self.finish_callback, None
            callback()

        self.request = None

        if disconnect == False :
            self.stream.read_until( b"\r\n\r\n", self.on_request_headers )

    def tryclose( self, disconnect=False ):
        """Try to close this connection. If `disconnect` is True,
        connection-stream is always closed, otherwise it follows HTTP
        guidelines to close the connection.""" 
        if disconnect == False and self.request :
            meth, hdrs = self.request.method, self.request.headers
            conn_val = h.connection( hdrs.get( "connection", None ))
            if self.supports_http_1_1() :
                disconnect = conn_val == [ b'close' ]
            elif ("content-length" in hdrs) or (meth in (b"HEAD", b"GET") ) :
                disconnect = conn_val != [ b'keep-alive' ]

        if disconnect == True :
            self.stream.close()
            if self.finish_callback : 
                callback = self.finish_callback
                self.finish_callback = None
                callback()
            self.server.close_connection( self )

        return disconnect

    #---- Callback handlers

    def on_write_complete( self ):
        """Local callback once response data is written."""
        if self.write_callback is not None:
            callback = self.write_callback
            self.write_callback = None
            callback()
        if self.request and self.request.has_finished() :
            self.dofinish()

    def on_request_headers( self, data ):
        """A request has started. Parse `data` for startline and headers."""
        if self.request != None :
            self.write( self.INTERNAL_ERROR, force=True )
            self.tryclose()
            self.pa.logerror(
                "Cannot start a new request when there is on-going request.." )
            if self['debug'] : raise

        # Remove empty-lines (CRLFs) prefixed to request message
        if data.strip( b'\r\n' ) == b'' :
            self.stream.read_until( b"\r\n\r\n", self.on_request_headers )
            return

        try :
            data = data.rstrip( b'\r\n' )
            # Get request-startline
            try :
                startline, hdrdata = data.split( b"\r\n", 1 )
            except ValueError :
                startline, hdrdata = data, b''

            method, uri, version = h.parse_startline( startline )
            if version != b"HTTP/1.1" :
                self.write( self.BAD_REQUEST, force=True )
                self.tryclose( disconnect=True )

            hdrs = h.HTTPHeaders.parse( hdrdata ) if hdrdata else []
            self.reqdata = ( method, uri, version, hdrs )

            # The presence of a message-body in a request is signaled by the
            # inclusion of a Content-Length or Transfer-Encoding header field
            # in the request's message-headers.
            clen = h.parse_content_length( hdrs.get( "content-length", None))
            transenc = h.parse_transfer_encoding( 
                            hdrs.get( 'transfer-encoding', None ))

            if transenc and transenc[0][0] == b'chunked' :
                hdrs.pop( "content-length", None )
                self.stream.read_until( b"\r\n", self.on_request_chunk_line )

            elif clen :
                expect = h.parse_expect( hdrs.get( "expect", None ))
                if clen > self['max_buffer_size'] :
                    self.write( self.ENTITY_LARGE, force=True )
                    self.stream_read_bytes( clen, self.on_skip_request )

                elif expect == "100-continue" :
                    self.write( b"HTTP/1.1 100 (Continue)\r\n\r\n",force=True )
                    self.stream.read_bytes( clen, self.on_request_body )

                else :
                    self.stream.read_bytes( clen, self.on_request_body )

            else :
                self.handle_request( *self.reqdata )
                self.stream.read_until( b"\r\n\r\n", self.on_request_headers )

        except h.Error as e:
            self.pa.logerror( "Malformed request ..." )
            self.write( self.BAD_REQUEST, force=True )
            self.tryclose( disconnect=True )
            if self['debug'] : raise

        return

    def on_skip_request( self, data ):
        """Skip `data`, may be it was detected that on-going request is
        malformed or cannot be supported."""
        self.stream.read_until( b"\r\n\r\n", self.on_request_headers )

    def on_request_body( self, data ):
        """Request body receivd. Dispatch request."""
        self.handle_request( *self.reqdata, body=data )
        self.stream.read_until( b"\r\n\r\n", self.on_request_headers )

    def on_request_chunk_line( self, data ):
        """A new Request chunk has started. We will receive only the
        chunk-line."""
        data = data.rstrip( b'\r\n' )
        hdrs = self.reqdata[3]
        try :
            chunk_size, chunk_ext = data.split( b';', 1 )
            chunk_size = int(chline, 16)
        except :
            chunk_size, chunk_ext = int(data, 16), None

        if chunk_size == 0 :    # last_chunk
            self.chunk = (chunk_size, chunk_ext, None)
            if h.parse_trailer( hdrs.get( 'trailer', None )) :# Trailer present
                self.stream.read_until( b"\r\n\r\n", self.on_request_trailer )
            else :
                self.stream.read_until( b"\r\n", self.on_request_chunks_done )
        else :
            self.reqdata[5] = (chunk_size, chunk_ext, None)
            # Increase chunk_size by 2, to include limiting CRLF.
            self.stream.read_bytes( chunk_size+2, self.on_request_chunk_data )

    def on_request_chunk_data( self, data ):
        """A request chunk is received."""
        chunk_size, chunk_ext, _ = self.chunk
        chunk = (chunk_size, chunk_ext, data[:-2])
        if self.request :
            self.handle_chunk( chunk=chunk )
        else :
            self.handle_request( *self.reqdata, chunk=chunk )
        self.stream.read_until( b"\r\n", self.on_request_chunk_line )

    def on_request_trailer( self, data ):
        """An optional trailer is received."""
        chunk_size, chunk_ext, _ = self.chunk
        chunk = (chunk_size, chunk_ext, None)
        ts = h.HTTPHeaders( data )
        if self.request :
            self.handle_chunk( chunk=chunk, trailers=ts )
        else :
            self.handle_request( *self.reqdata, chunk=chunk, trailers=ts )
        self.stream.read_until( b"\r\n\r\n", self.on_request_headers )

    def on_request_chunks_done( self, data ):
        """The last chunk was received without a trailer. Dispatch."""
        chunk_size, chunk_ext, _ = self.chunk
        chunk = (chunk_size, chunk_ext, data)
        if self.request :
            self.handle_chunk( chunk=chunk )
        else :
            self.handle_request( *self.reqdata, chunk=chunk )
        self.stream.read_until( b"\r\n\r\n", self.on_request_headers )

    def on_timeout( self ):
        """The connection was idle and a timeout has occured. Close the
        connection."""
        self.tryclose( disconnect=True )

    def on_connection_close( self ):
        """This method will be invoked by the stream object when socket is
        closed. In return this connection must call any subscribed callback.
        """
        if self.close_callback is not None:
            callback = self.close_callback
            self.close_callback = None
            callback()
        self.tryclose( disconnect=True )

    #---- ISettings interface methods

    @classmethod
    def default_settings( cls ):
        return _ds2

    @classmethod
    def normalize_settings( cls, sett ):
        sett['no_keep_alive'] = \
                h.asbool( sett['no_keep_alive'], _ds2['no_keep_alive'] )
        sett['max_buffer_size'] = \
                h.asint( sett['max_buffer_size'], _ds2['max_buffer_size'] )
        sett['read_chunk_size'] = \
                h.asint( sett['read_chunk_size'], _ds2['read_chunk_size'] )
        return sett


class Timeout( object ):
    """Heapable timeout, a UNIX timestamp and a callback"""

    # Reduce memory overhead when there are lots of pending callbacks
    __slots__ = ['deadline', 'callback']

    def __init__( self, deadline, callback ):
        if isinstance( deadline, (int, float) ):
            self.deadline = deadline
        elif isinstance( deadline, datetime.timedelta ):
            self.deadline = time.time() + h.timedelta_to_seconds(deadline)
        else:
            raise TypeError( "Unsupported deadline %r" % deadline )
        self.callback = callback

    # Comparison methods to sort by deadline, with object id as a tiebreaker
    # to guarantee a consistent ordering.  The heapq module uses __le__
    # in python2.5, and __lt__ in 2.6+ (sort() and most other comparisons
    # use __lt__).
    def __lt__( self, other ):
        return ( (self.deadline, id(self)) < (other.deadline, id(other)) )

    def __le__( self, other ):
        return ( (self.deadline, id(self)) <= (other.deadline, id(other)) )


class Waker( object ):
    """A dummy file-descriptor watched by event-poll. To wake up the epoll as
    we desire."""

    def __init__(self):
        r, w = os.pipe()
        h.set_nonblocking(r, w)
        h.set_close_exec(r, w)
        self.reader = os.fdopen(r, "rb", 0)
        self.writer = os.fdopen(w, "wb", 0)

    def fileno(self):
        return self.reader.fileno()

    def wake(self):
        try           : self.writer.write( b'x' )
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


class IOStream( object ):
    """A utility class to write to and read from a non-blocking socket.

    We support a non-blocking ``write()`` and a family of ``read_*()`` 
    methods.  All of the methods take callbacks (since writing and reading are
    non-blocking and asynchronous).

    The socket is a resulting connected socket via socket.accept()."""

    httpconn = None
    """:class:`IHTTPConnection` plugin."""

    conn = None
    """Socket object (accepted by the server) to stream data."""

    address = None
    """socket address for the other end of connection."""

    ioloop = None
    """Event loop for epoll service."""

    server = None
    """Server plugin instance."""

    _read_buffer = None
    """A collection object to buffer read bytes from socket."""

    _write_buffer = None
    """A collection object to buffer bytes to be written to socket."""

    _read_buffer_size = 0
    """Indicates the size of available read data in _read_buffer."""

    _write_buffer_frozen = False

    _read_delimiter = None
    """stream reads data from the socket until this delimiter is detected."""

    _read_regex = None
    """stream reads data from the socket until a pattern specified by this
    regular expression is detected"""

    _read_bytes = None
    """stream reads specified number of bytes from the socket."""

    _read_until_close = False
    """stream reads data from the socket until the socket is closed."""

    _read_callback = None
    """Call back for one of the read*() APIs."""

    _streaming_callback = None
    """In case of reading specified number of bytes or read until close, this 
    callback is called for every chunk that are read."""

    _write_callback = None
    """Call back for one of the write*() APIs."""

    _close_callback = None
    """Call back when socket is closed."""

    _state = None
    """IO Events for which this connection is polled for."""

    _pending_callbacks = 0

    def __init__( self, httpconn ):
        self.httpconn = httpconn
        self.conn = httpconn.conn
        self.address = httpconn.address
        self.server = httpconn.server
        self.ioloop = self.server.ioloop

        self.conn.setblocking( False )

        # configuration settings
        self.max_buffer_size = httpconn['max_buffer_size']
        self.read_chunk_size = httpconn['read_chunk_size']

        self._read_buffer = collections.deque()
        self._write_buffer = collections.deque()
        self._read_buffer_size = 0
        self._write_buffer_frozen = False
        self._read_delimiter = None
        self._read_regex = None
        self._read_bytes = None
        self._read_until_close = False
        self._read_callback = None
        self._streaming_callback = None
        self._write_callback = None
        self._close_callback = None
        self._state = None
        self._pending_callbacks = 0

    #---- API methods.

    def read_until_regex(self, regex, callback):
        """Call callback when we read the given regex pattern. Callback
        is passed with binary data."""
        self.set_read_callback( callback )
        self._read_regex = re.compile( regex )
        self.tryread()

    def read_until(self, delimiter, callback):
        """Call callback when we read the given delimiter. Callback is passed
        with binary data."""
        self.set_read_callback( callback )
        self._read_delimiter = delimiter
        self.tryread()

    def read_bytes( self, num_bytes, callback, streaming_callback=None ):
        """Call callback when we read the given number of bytes. Callback is
        passed with binary data.

        If a ``streaming_callback`` is given, it will be called with chunks
        of data as they become available, and the argument to the final
        ``callback`` will be empty.
        """
        self.set_read_callback( callback )
        self._read_bytes = num_bytes
        self._streaming_callback = streaming_callback
        self.tryread()

    def read_until_close( self, callback, streaming_callback=None ):
        """Reads all data from the socket until it is closed. Callback is
        passed with binary data.

        If a ``streaming_callback`` is given, it will be called with chunks
        of data as they become available, and the argument to the final
        ``callback`` will be empty.

        Subject to ``max_buffer_size`` limit if a ``streaming_callback`` is 
        not used.
        """
        self.set_read_callback(callback)

        if self.closed() : # Already closed.
            self.docallback( self._read_buffer_size, callback )
            return

        self._read_until_close = True
        self._streaming_callback = streaming_callback
        self.add_io_state( self.ioloop.READ )

    def write( self, data, callback=None ):
        """Write the given data to this stream. `data` is expected to be in
        bytes.

        If callback is given, we call it when all of the buffered write
        data has been successfully written to the stream. If there was
        previously buffered write data and an old write callback, that
        callback is simply overwritten with this new callback.
        """
        self.check_closed()
        if data :
            # We use bool(_write_buffer) as a proxy for write_buffer_size>0,
            # so never put empty strings in the buffer.
            self._write_buffer.append( data )
        self._write_callback = callback
        self.handle_write()
        if self._write_buffer :
            self.add_io_state( self.ioloop.WRITE )
        self.maybe_add_error_listener()

    def set_close_callback( self, callback ):
        """Call the given callback when the stream is closed."""
        self._close_callback = callback

    def close( self ):
        """Close this stream."""
        if self.server['debug'] == True :
            self.server.pa.loginfo(
                    "Closing the stream for %r" % (self.address,) )

        if self.conn is not None:
            if self._read_until_close :
                self.docallback( self._read_buffer_size, self._read_callback )
            if self._state is not None:
                self.ioloop.remove_handler( self.conn.fileno() )
                self._state = None
            self.conn.close()
            self.conn = None

        self.try_close_callback()

    def reading(self):
        """Returns true if we are currently reading from the stream."""
        return self._read_callback is not None

    def writing(self):
        """Returns true if we are currently writing to the stream."""
        return bool(self._write_buffer)

    def closed(self):
        """Returns true if the stream has been closed."""
        return self.conn is None

    #---- Local methods.

    def try_close_callback(self):
        """If a close-callback is subscribed, try calling back.  If there are
        pending callbacks, don't run the close callback until they're done
        (see _maybe_add_error_handler)."""
        if ( self.conn is None and self._close_callback and
             self._pending_callbacks == 0 ):

            cb = self._close_callback
            self._close_callback = None
            run_callback( self.server, cb )

    def docallback( self, consume_bytes, callback ):
        """There can be only one callback subscribed on a read. Either for 
             read_bytes(), read_until(), read_until_regex(),
             read_until_close()
        """
        self._read_bytes = None
        self._read_delimiter = None
        self._read_regex = None
        self._read_until_close = False

        self._read_callback = None
        self._streaming_callback = None

        data = self.consume( consume_bytes )
        run_callback( self.server, callback, data )

    def on_epoll_event( self, fd, events ):
        """Callback for this socket's (conn's) events monitored by an EPoll."""

        if not self.conn:
            self.server.pa.logwarn( "Got events for closed stream %d" % fd )
            return

        try:
            if events & self.ioloop.READ :
                self.handle_read()
            if not self.conn : return

            if events & self.ioloop.WRITE :
                self.handle_write()
            if not self.conn: return

            if events & self.ioloop.ERROR:
                # We may have queued up a user callback in handle_read or
                # handle_write, so don't close the HTTPIOStream until those
                # callbacks have had a chance to run.
                self.ioloop.add_callback( self.close )
                return

            state = self.ioloop.ERROR
            if self.reading() :
                state |= self.ioloop.READ
            if self.writing() :
                state |= self.ioloop.WRITE
            if state == self.ioloop.ERROR :
                state |= self.ioloop.READ

            if state != self._state:
                if self._state is not None :
                    raise Exception( 
                            "shouldn't happen: on_epoll_event without _state" )
                self._state = state
                self.ioloop.update_handler( self.conn.fileno(), self._state )
        except Exception :
            self.server.pa.logerror( 
                    "Uncaught exception, closing connection." )
            self.close()
            if self.server['debug'] : raise

    def handle_read( self ):
        try:
            try:
                # Pretend to have a pending callback so that an EOF in
                # read_to_buffer() doesn't trigger an immediate close
                # callback.  At the end of this method we'll either
                # estabilsh a real pending callback via # read_from_buffer or
                # run the close callback
                
                # We need two try statements here so that
                # pending_callbacks is decremented before the `except`
                # clause below (which calls `close` and does need to
                # trigger the callback)
                self._pending_callbacks += 1
                while True:
                    # Read from the socket until we get EWOULDBLOCK or
                    # equivalent. SSL sockets do some internal buffering, and
                    # if the data is sitting in the SSL object's buffer
                    # select() and friends can't see it; the only way to find
                    # out if it's there is to try to read it.
                    if self.read_to_buffer() == 0:
                        break
            finally:
                self._pending_callbacks -= 1
        except Exception:
            self.server.pa.logwarn( "error on read" )
            self.close()
            if self['debug'] : raise
            return

        if self.read_from_buffer() :
            return
        else:
            self.try_close_callback()

    def read_from_socket( self ):
        """Attempts to read from the socket.
        Returns the data read or None if there is nothing to read.
        """
        try:
            chunk = self.conn.recv( self.read_chunk_size )
        except socket.error as e:
            if e.args[0] in (errno.EWOULDBLOCK, errno.EAGAIN):
                return None
            raise
        if not chunk : # May be the remote end closed
            self.close()
            return None
        return chunk

    def read_to_buffer(self):
        """Reads from the socket and appends the result to the read buffer.

        Returns the number of bytes read.  Returns 0 if there is nothing
        to read (i.e. the read returns EWOULDBLOCK or equivalent).  On
        error closes the socket and raises an exception.
        """
        try:
            chunk = self.read_from_socket()
        except socket.error as e:
            # ssl.SSLError is a subclass of socket.error
            err = "Read error on %d: %s" % (self.conn.fileno(), e)
            self.server.pa.logwarn( err )
            self.close()
            if self['debug'] : raise

        if chunk is None: return 0

        self._read_buffer.append( chunk )
        self._read_buffer_size += len(chunk)
        if self._read_buffer_size >= self.max_buffer_size :
            err = "Reached maximum read buffer size"
            self.close()
            self.server.pa.logerror( err )
            raise IOError( err )

        return len(chunk)

    def read_from_buffer(self):
        """Attempts to complete the currently-pending read from the buffer.

        Returns True if the read was completed and the callback registered via
        on of the API is issued and returned.
        """

        # Do streaming callback, in case of read_bytes() API, for chunked
        # read.
        if self._streaming_callback is not None and self._read_buffer_size :
            bytes_to_consume = self._read_buffer_size
            if self._read_bytes is not None :
                bytes_to_consume = min(self._read_bytes, bytes_to_consume)
                self._read_bytes -= bytes_to_consume
            data = self.consume( bytes_to_consume )
            run_callback( self.server, self._streaming_callback, data )

        # For read_bytes() API
        if ( self._read_bytes != None and 
                self._read_buffer_size >= self._read_bytes ) :

            self.docallback( self._read_bytes, self._read_callback )
            return True

        # For read_until() API
        elif self._read_delimiter is not None and self._read_buffer:
            # Multi-byte delimiters (e.g. '\r\n') may straddle two
            # chunks in the read buffer, so we can't easily find them
            # without collapsing the buffer.  However, since protocols
            # using delimited reads (as opposed to reads of a known
            # length) tend to be "line" oriented, the delimiter is likely
            # to be in the first few chunks.  Merge the buffer gradually
            # since large merges are relatively expensive and get undone in
            # consume().
            while True:
                loc = self._read_buffer[0].find( self._read_delimiter )
                if loc != -1 :  # Do callback
                    l = loc + len(self._read_delimiter)
                    self.docallback( l, self._read_callback )
                    return True

                if len(self._read_buffer) == 1: # Let us wait for more data.
                    break

                # Join with next chunk and try to find a delimiter.
                self.double_prefix( self._read_buffer )

        # For read_until_regex() API
        elif self._read_regex is not None and self._read_buffer :
            while True:
                m = self._read_regex.search( self._read_buffer[0] )
                if m is not None:
                    self.docallback( m.end(), self._read_callback )
                    return True

                if len(self._read_buffer) == 1:
                    break

                self.double_prefix(self._read_buffer)

        return False

    def set_read_callback(self, callback):
        assert not self._read_callback, "Already reading"
        self._read_callback = callback

    def tryread(self):
        """Attempt to complete the current read operation from buffered data.

        If the read can be completed without blocking, schedules the
        read callback on the next IOLoop iteration; otherwise starts
        listening for reads on the socket.
        """
        # See if we've already got the data from a previous read
        if self.read_from_buffer() : return

        # If the socket is not closed, then try reading from the socket.
        self.check_closed()
        while True :
            if self.read_to_buffer() == 0 : break
            self.check_closed()

        # And see if we've already got the data from this read
        if self.read_from_buffer(): return

        self.add_io_state( self.ioloop.READ )

    def handle_write(self):
        while self._write_buffer:
            try:
                if not self._write_buffer_frozen :
                    # On windows, socket.send blows up if given a
                    # write buffer that's too large, instead of just
                    # returning the number of bytes it was able to
                    # process.  Therefore we must not call socket.send
                    # with more than 128KB at a time.
                    self.merge_prefix(self._write_buffer, 128 * 1024)
                num_bytes = self.conn.send(self._write_buffer[0])
                if num_bytes == 0:
                    # With OpenSSL, if we couldn't write the entire buffer,
                    # the very same string object must be used on the
                    # next call to send.  Therefore we suppress
                    # merging the write buffer after an incomplete send.
                    # A cleaner solution would be to set
                    # SSL_MODE_ACCEPT_MOVING_WRITE_BUFFER, but this is
                    # not yet accessible from python
                    # (http://bugs.python.org/issue8240)
                    self._write_buffer_frozen = True
                    break
                self._write_buffer_frozen = False
                self.merge_prefix(self._write_buffer, num_bytes)
                self._write_buffer.popleft()
            except socket.error as e:
                if e.args[0] in (errno.EWOULDBLOCK, errno.EAGAIN):
                    self._write_buffer_frozen = True
                    break
                else:
                    err = "Write error on %d: %s" % (self.conn.fileno(), e)
                    self.server.pa.logwarn( err )
                    self.close()
                    if self['debug'] : raise
                    return

        if not self._write_buffer and self._write_callback :
            callback = self._write_callback
            self._write_callback = None
            run_callback( self.server, callback )

    def consume( self, loc ):
        if loc == 0:
            return b""
        self.merge_prefix( self._read_buffer, loc )
        self._read_buffer_size -= loc
        return self._read_buffer.popleft()

    def check_closed(self):
        if not self.conn: raise IOError("Stream is closed")

    def maybe_add_error_listener( self ):
        if self._state is None and self._pending_callbacks == 0:
            if self.conn is None:
                self.try_close_callback()
            else:
                self.add_io_state( self.ioloop.READ )

    def add_io_state(self, state):
        """Adds `state` (IOLoop.{READ,WRITE} flags) to our event handler.

        Implementation notes: Reads and writes have a fast path and a
        slow path.  The fast path reads synchronously from socket
        buffers, while the slow path uses `add_io_state()` to schedule
        an IOLoop callback.  Note that in both cases, the callback is
        run asynchronously with `_run_callback`.

        To detect closed connections, we must have called
        `add_io_state()` at some point, but we want to delay this as
        much as possible so we don't have to set an `IOLoop.ERROR`
        listener that will be overwritten by the next slow-path
        operation.  As long as there are callbacks scheduled for
        fast-path ops, those callbacks may do more reads.
        If a sequence of fast-path ops do not end in a slow-path op,
        (e.g. for an @asynchronous long-poll request), we must add
        the error handler.  This is done in `_run_callback` and `write`
        (since the write callback is optional so we can have a
        fast-path write with no `_run_callback`)
        """
        # Check for closed connection.
        if self.conn is None : return

        if self._state is None :
            self._state = self.ioloop.ERROR | state
            self.ioloop.add_handler(
                    self.conn.fileno(), self.on_epoll_event, self._state )

        elif not self._state & state :
            self._state = self._state | state
            self.ioloop.update_handler( self.conn.fileno(), self._state )

    def double_prefix( self, deque ):
        """Grow by doubling, but don't split the second chunk just because the
        first one is small.
        """
        new_len = max( len(deque[0]) * 2, (len(deque[0]) + len(deque[1])) )
        self.merge_prefix( deque, new_len )

    def merge_prefix( self, deque, size ):
        """Replace the first entries in a deque of strings with a single
        string of up to size bytes.

        >>> d = collections.deque(['abc', 'de', 'fghi', 'j'])
        >>> merge_prefix(d, 5); print d
        deque(['abcde', 'fghi', 'j'])

        Strings will be split as necessary to reach the desired size.
        >>> merge_prefix(d, 7); print d
        deque(['abcdefg', 'hi', 'j'])

        >>> merge_prefix(d, 3); print d
        deque(['abc', 'defg', 'hi', 'j'])

        >>> merge_prefix(d, 100); print d
        deque(['abcdefghij'])
        """
        if len(deque) == 1 and len(deque[0]) <= size:
            return
        prefix = []
        remaining = size
        while deque and remaining > 0:
            chunk = deque.popleft()
            if len(chunk) > remaining:
                deque.appendleft(chunk[remaining:])
                chunk = chunk[:remaining]
            prefix.append(chunk)
            remaining -= len(chunk)
        # This data structure normally just contains byte strings, but
        # the unittest gets messy if it doesn't use the default str() type,
        # so do the merge based on the type of data that's actually present.
        if prefix:
            deque.appendleft(type(prefix[0])().join(prefix))
        if not deque:
            deque.appendleft(b"")


class SSLIOStream( IOStream ):
    """A utility class to write to and read from a non-blocking SSL socket.

    If a dictionary is provided as keyword argument ssl-options,
    it will be used as additional keyword arguments to ssl.wrap_socket.
    """

    def __init__( self, httpconn ):
        self.ssloptions = h.settingsfor( 'ssl.', httpconn.server )
        super().__init__( httpconn )
        self._ssl_accepting = True
        self._handshake_reading = False
        self._handshake_writing = False

    def reading(self):
        return self._handshake_reading or super().reading(self)

    def writing(self):
        return self._handshake_writing or super().writing(self)

    def _do_ssl_handshake(self):
        # Based on code from test_ssl.py in the python stdlib
        try:
            self._handshake_reading = False
            self._handshake_writing = False
            self.conn.do_handshake()
        except ssl.SSLError as err:
            if err.args[0] == ssl.SSL_ERROR_WANT_READ:
                self._handshake_reading = True
                return
            elif err.args[0] == ssl.SSL_ERROR_WANT_WRITE:
                self._handshake_writing = True
                return
            elif err.args[0] in (ssl.SSL_ERROR_EOF,
                                 ssl.SSL_ERROR_ZERO_RETURN):
                return self.close()
            elif err.args[0] == ssl.SSL_ERROR_SSL:
                self.server.pa.logwarn( 
                        "SSL Error on %d: %s" % (self.conn.fileno(), err) )
                return self.close()
            raise
        except socket.error as err:
            if err.args[0] == errno.ECONNABORTED:
                return self.close()
            raise

    def handle_read(self):
        if self._ssl_accepting:
            self._do_ssl_handshake()
            return
        super().handle_read()

    def handle_write(self):
        if self._ssl_accepting:
            self._do_ssl_handshake()
            return
        super().handle_write()

    def read_from_socket(self):
        if self._ssl_accepting:
            # If the handshake hasn't finished yet, there can't be anything
            # to read (attempting to read may or may not raise an exception
            # depending on the SSL version)
            return None
        try:
            # SSLSocket objects have both a read() and recv() method,
            # while regular sockets only have recv().
            # The recv() method blocks (at least in python 2.6) if it is
            # called when there is nothing to read, so we have to use
            # read() instead.
            chunk = self.conn.read( self.read_chunk_size )
        except ssl.SSLError as e:
            # SSLError is a subclass of socket.error, so this except
            # block must come first.
            if e.args[0] == ssl.SSL_ERROR_WANT_READ:
                return None
            raise
        except socket.error as e:
            if e.args[0] in (errno.EWOULDBLOCK, errno.EAGAIN):
                return None
            raise
        if not chunk :
            self.close()
            return None
        return chunk

