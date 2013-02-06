# -*- coding: utf-8 -*-

# This file is subject to the terms and conditions defined in
# file 'LICENSE', which is part of this source code package.
#       Copyright (c) 2011 R Pratap Chakravarthy

"""Platform can be configured via ini files. For ease of administration,
platform can be configured via web as well, where the configuration
information (basically the key, value pair) will be persisted by a backend
store like sqlite3.

Note that configuration parameters from database backend will override 
default-configuration and configurations from ini file.
"""

import sqlite3

from   pluggdapps.plugin      import Plugin, implements
from   pluggdapps.interfaces  import IConfigDB
import pluggdapps.utils       as h

_default_settings = h.ConfigDict()
_default_settings.__doc__ = (
    "Backend interface to persist configuration information in sqlite "
    "database." )

_default_settings['url'] = {
    'default' : '',
    'types'   : (str,),
    'help'    : "Location of sqlite3 backend file. Will be passed to "
                "sqlite3.connect() API."
}

class ConfigSqlite3DB( Plugin ):
    implements( IConfigDB )

    def __init__( self ):
        self.conn = sqlite3.connect( self['url'] ) if self['url'] else None

    def connect( self, *args, **kwargs ):
        """:meth:`pluggdapps.interfaces.IConfigDB.connect` interface method."""
        if self.conn == None and self['url'] :
            self.conn = sqlite3.connect( self['url'] )

    def dbinit( self, netpaths=[] ):
        """:meth:`pluggdapps.interfaces.IConfigDB.dbinit` interface method.
        
        Optional key-word argument,

        ``netpaths``,
            list of web-application mount points. A table for each netpath
            will be created.
        """
        if self.conn == None : return None

        c = self.conn.cursor()
        # Create the `platform` table if it does not exist.
        c.execute(
            "CREATE TABLE IF NOT EXISTS platform "
                "(section TEXT PRIMARY KEY ASC, settings TEXT);" )
        self.conn.commit()

        for netpath in netpaths :
            sql = ( "CREATE TABLE IF NOT EXISTS '%s' "
                        "(section TEXT PRIMARY KEY ASC, settings TEXT);" ) %\
                  netpath
            c.execute( sql )
            self.conn.commit()

    def config( self, **kwargs ):
        """:meth:`pluggdapps.interfaces.IConfigDB.config` interface method.

        - if netpath, section, name and value kwargs are supplied, will update
          config-parameter name under webapp's section with value.
        - if netpath, section, name kwargs are supplied, will return
          configuration value for name under webapp's section.
        - if netpath, section kwargs are supplied, will return dictionary of 
          all configuration parameters under webapp's section.
        - if netpath is supplied, will return dictionary of section
          configuration.
        - if netpath is not supplied, will assume platform configuration.

        Keyword arguments,

        ``netpath``,
            Netpath, including hostname and script-path, on which
            web-application is mounted. Optional.

        ``section``,
            Section name to get or set config parameter. Optional.

        ``name``,
            Configuration name to get or set for ``section``. Optional.

        ``value``,
            If present, this method was invoked for setting configuration
            ``name`` under ``section``. Optional.
        """
        if self.conn == None : return None

        netpath = kwargs.get( 'netpath', 'platform' )
        section = kwargs.get( 'section', None )
        name = kwargs.get( 'name', None )
        value = kwargs.get( 'value', None )

        c = self.conn.cursor()
        if section :
            c.execute('SELECT * FROM %s WHERE section=%s' % (netpath,section))
            secsetts = h.json_decode( list(c)[0][1] ) or {}
            if secsetts and name and value :
                secsetts[name] = value
                c.execute( 'INSERT INTO %s VALUES (%s, %s)' %
                           (netpath, section, h.json_encode(secsetts)) )
                self.conn.commit()
                rc = value
            elif secsetts and name :
                rc = secsetts[name]
            else :
                rc = secsetts
        else :
            c.execute( "SELECT * FROM '%s'" % (netpath,) )
            settings = {}
            for section, setts in list(c) :
                settings[ section ] = h.json_decode( setts )
            rc = settings
        return rc

    def close( self ):
        """:meth:`pluggdapps.interfaces.IConfigDB.close` interface method."""
        if self.conn :
            self.conn.close()

    #---- ISettings interface methods

    @classmethod
    def default_settings( cls ):
        """:meth:`pluggdapps.plugin.ISettings.default_settings` interface
        method.
        """
        return _default_settings

    @classmethod
    def normalize_settings( cls, sett ):
        """:meth:`pluggdapps.plugin.ISettings.normalize_settings` interface
        method.
        """
        return sett
