# -*- coding: utf-8 -*-

import pluggdapps.utils             as h
from   pluggdapps.plugin            import implements, Plugin
from   pluggdapps.web.webinterfaces import IHTTPResource

class {webapp_name}Resource( Plugin ):
    implements( IHTTPResource )

    def __call__( request, c ):
        pass
