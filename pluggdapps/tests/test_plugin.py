import unittest
from   random import choice

from   pluggdapps.plugin     import *
from   pluggdapps.interfaces import ICommand, IWebApp, IHTTPServer
from   pluggdapps.web.webinterfaces import IHTTPView, IHTTPCookie, \
                                           IHTTPRenderer, IHTTPRequest, \
                                           IHTTPResource, IHTTPResponse, \
                                           IHTTPOutBound, \
                                           IHTTPRouter

class TestPlugin( unittest.TestCase ):

    def test_isimplement( self ):
        from pluggdapps.plugin import PluginMeta
        ls = query_plugin( None, ICommand, 'commandls' )
        assert isimplement( ls, ICommand )
        assert isimplement( ls, ISettings )
        assert not isimplement( ls, IWebApp )

    def test_interfaces( self ):
        ref = [ ICommand, IHTTPView, IHTTPCookie, IHTTPRenderer,
                IHTTPRequest, IHTTPRouter, IHTTPResource, IHTTPResponse,
                IHTTPOutBound, IHTTPServer, ISettings, IWebApp ]
        fn = lambda x : x.__name__
        assert sorted( interfaces(), key=fn ) == sorted( ref, key=fn )

    def test_interface( self ):
        assert interface( 'ICommand' ) == ICommand

    def test_plugin_info( self ):
        from pluggdapps.commands.ls import CommandLs
        ls = query_plugin( None, ICommand, 'commandls' )
        x = plugin_info( 'commandls' )
        assert x['cls'] == CommandLs
        x = plugin_info( CommandLs )
        assert x['cls'] == CommandLs
        x = plugin_info( ls )
        assert x['cls'] == CommandLs

    def test_interface_info( self ):
        x = interface_info( 'ICommand' )
        assert x['cls'] == ICommand
        x = interface_info( ICommand )
        assert x['cls'] == ICommand

    def test_pluginnames( self ):
        plugins = pluginnames()
        assert 'baseresource' in plugins
        assert 'commandcommands' in plugins
        assert 'commandconfig' in plugins
        assert 'commandls' in plugins
        assert 'commandmounts' in plugins
        assert 'commandserve' in plugins
        assert 'commandunittest' in plugins
        assert 'httprequest' in plugins
        assert 'httpresponse' in plugins
        assert 'plugin' in plugins
        assert 'rootapp' in plugins
        assert 'webapp' in plugins

        plugins = pluginnames( ICommand )
        assert 'commandcommands' in plugins
        assert 'commandconfig' in plugins
        assert 'commandls' in plugins
        assert 'commandmounts' in plugins
        assert 'commandserve' in plugins
        assert 'commandunittest' in plugins
        assert 'webapp' not in plugins

    def test_pluginclass( self ):
        from pluggdapps.commands.ls import CommandLs
        assert pluginclass( ICommand, 'commandls' ) == CommandLs

    def test_applications( self ):
        assert 'webapp' in applications()
        assert 'rootapp' in applications()

    def test_plugins( self ):
        plugins_ = plugins()
        assert 'baseresource' in plugins_
        assert 'commandcommands' in plugins_
        assert 'commandconfig' in plugins_
        assert 'commandls' in plugins_
        assert 'commandmounts' in plugins_
        assert 'commandserve' in plugins_
        assert 'commandunittest' in plugins_
        assert 'httprequest' in plugins_
        assert 'httpresponse' in plugins_
        assert 'plugin' in plugins_
        assert 'rootapp' in plugins_
        assert 'webapp' in plugins_

    def test_whichmodule( self ):
        assert whichmodule(TestPlugin).__name__=='pluggdapps.tests.test_plugin'
        assert whichmodule(self).__name__ == 'pluggdapps.tests.test_plugin'
        assert whichmodule(whichmodule).__name__ == 'pluggdapps.plugin'

    def test_pluginname( self ):
        from  pluggdapps.commands.ls import CommandLs
        o = query_plugin( None, ICommand, 'commandls' )
        assert pluginname( 'CommandLs' ) == 'commandls' 
        assert pluginname( CommandLs ) == 'commandls'
        assert pluginname( o ) == 'commandls'

    def test_pluginmeta( self ):
        from pluggdapps.plugin import PluginMeta, Interface, PluginBase, \
                                      Singleton, plugin_init
        from pprint import pprint

        fn = lambda x : x.__name__
        refs = [
                'baseresource', 'commandcommands', 'commandconfig',
                'commandls', 'commandmounts', 'commandserve', 'commandunittest',
                'httprequest', 'httpresponse', 'plugin', 'rootapp', 'webapp' ]
        for r in refs : assert r in PluginMeta._pluginmap.keys()

        refs = [ ICommand, IHTTPView, IHTTPRouter, IHTTPCookie,
                 IHTTPRenderer, IHTTPRequest, IHTTPResource, IHTTPResponse, 
                 IHTTPOutBound, IHTTPServer ]
        refs = map( fn,  refs )
        for r in refs : assert r in PluginMeta._interfmap.keys()

        m = {}
        for k in PluginMeta._implementers.keys() :
            for x,y in PluginMeta._implementers[k].items() :
                m.setdefault( y, [] ).append( k )
        for y in plugins() :
            cls = PluginMeta._pluginmap[y]['cls']
            assert sorted(cls._interfs, key=fn) == sorted(m[cls], key=fn)

        assert not hasattr( Interface, '_interfs' )
        assert not hasattr( PluginBase, '_interfs' )

        # Test Plugin's __init__ chain and masterinit
        class A( Plugin ):
            def __init__( self, a ):
                self.varA = a * 2

        class B( A ):
            def __init__( self, a ):
                self.varB = a * 3
                self._super_init( __class__, a )

        class C( B ):
            pass

        plugin_init()

        a = query_plugin( None, ISettings, 'a', 10 )
        b = query_plugin( None, ISettings, 'b', 20 )
        c = query_plugin( None, ISettings, 'c', 30 )

        assert a.varA == 20
        assert b.varA == 40 and  b.varB == 60
        assert c.varA == 60 and  c.varB == 90

    def test_singleton( self ):
        class A( Plugin ):
            def __init__( self, a ):
                self.varA = a * 2

        class D( Singleton ):
            pass

        plugin_init()

        a = query_plugin( None, ISettings, 'a', 10 )
        b = query_plugin( None, ISettings, 'a', 20 )
        assert id(a) != id(b)
        a = query_plugin( None, ISettings, 'd' )
        b = query_plugin( None, ISettings, 'd' )
        assert id(a) == id(b)


if __name__ == '__main__' :
    unittest.main()
