import logging
import Globals

from lxml import etree
from diazo.compiler import compile_theme

from repoze.xmliter.utils import getHTMLSerializer

from zope.interface import implements, Interface
from zope.component import adapts
from zope.component import queryUtility
from zope.site.hooks import getSite

from plone.registry.interfaces import IRegistry
from plone.transformchain.interfaces import ITransform

from plone.app.theming.interfaces import IThemeSettings, IThemingLayer,\
    IDomainToTheme
from plone.app.theming.utils import expandAbsolutePrefix, PythonResolver, InternalResolver, NetworkResolver

LOGGER = logging.getLogger('plone.app.theming')

class _Cache(object):
    """Simple cache for the transform
    """
    
    def __init__(self):
        self.transform = None
    
    def updateTransform(self, transform):
        self.transform = transform

def getCache(settings, key):
    # We need a persistent object to hang a _v_ attribute off for caching.
    
    registry = settings.__registry__
    caches = getattr(registry, '_v_plone_app_theming_caches', None)
    if caches is None:
        caches = registry._v_plone_app_theming_caches = {}
    cache = caches.get(key)
    if cache is None:
        cache = caches[key] = _Cache()
    return cache

def invalidateCache(settings, event):
    """When our settings are changed, invalidate the cache on all zeo clients
    """
    registry = settings.__registry__
    registry._p_changed = True
    if hasattr(registry, '_v_plone_app_theming_caches'):
        del registry._v_plone_app_theming_caches

class ThemeTransform(object):
    """Late stage in the 8000's transform chain. When plone.app.blocks is
    used, we can benefit from lxml parsing having taken place already.
    """
    
    implements(ITransform)
    adapts(Interface, IThemingLayer)
    
    order = 8850
    
    def __init__(self, published, request):
        self.published = published
        self.request = request

    def setupTransform(self):
        request = self.request
        DevelopmentMode = Globals.DevelopmentMode
        
        # Look for off switch if we are in debug mode
        if (DevelopmentMode and 
            request.get('diazo.off', '').lower() in ('1', 'y', 'yes', 't', 'true')
        ):
            return None
        
        # Find the host name
        base1 = request.get('BASE1')
        _, base1 = base1.split('://', 1)
        host = base1.lower()
        
        # Make sure it's always possible to see an unstyled page
        if (DevelopmentMode and
            (host == '127.0.0.1' or host == '127.0.0.1:%s' % request.get('SERVER_PORT'))
        ):
            return None
        
        # Obtain settings. Do nothing if not found
        
        settings = self.getSettings()
        if settings is None:
            return None

        rules = settings.rules
        if not rules:
            return None
        
        try:
            key = getSite().absolute_url()
        except AttributeError:
            return None
        cache = getCache(settings, key)
        
        # Apply theme
        transform = None
        
        if not DevelopmentMode:
            transform = cache.transform
        
        if transform is None:
            absolutePrefix = settings.absolutePrefix or None
            readNetwork = settings.readNetwork
            accessControl = etree.XSLTAccessControl(read_file=True, write_file=False, create_dir=False, read_network=readNetwork, write_network=False)
            
            if absolutePrefix:
                absolutePrefix = expandAbsolutePrefix(absolutePrefix)
            
            internalResolver = InternalResolver()
            pythonResolver = PythonResolver()
            if readNetwork:
                networkResolver = NetworkResolver()
            
            rulesParser = etree.XMLParser(recover=False)
            rulesParser.resolvers.add(internalResolver)
            rulesParser.resolvers.add(pythonResolver)
            if readNetwork:
                rulesParser.resolvers.add(networkResolver)
            
            themeParser = etree.HTMLParser()
            themeParser.resolvers.add(internalResolver)
            themeParser.resolvers.add(pythonResolver)
            if readNetwork:
                themeParser.resolvers.add(networkResolver)
            
            compilerParser = etree.XMLParser()
            compilerParser.resolvers.add(internalResolver)
            compilerParser.resolvers.add(pythonResolver)
            if readNetwork:
                compilerParser.resolvers.add(networkResolver)
            
            compiledTheme = compile_theme(rules,
                    absolute_prefix=absolutePrefix,
                    parser=themeParser,
                    rules_parser=rulesParser,
                    compiler_parser=compilerParser,
                    read_network=readNetwork,
                    access_control=accessControl,
                    update=False,
                )
            
            if not compiledTheme:
                return None
            
            transform = etree.XSLT(compiledTheme,
                    access_control=accessControl,
                )
            
            if not DevelopmentMode:
                cache.updateTransform(transform)
        
        return transform
    
    
    def parseTree(self, result):
        contentType = self.request.response.getHeader('Content-Type')
        if contentType is None or not contentType.startswith('text/html'):
            return None
        
        contentEncoding = self.request.response.getHeader('Content-Encoding')
        if contentEncoding and contentEncoding in ('zip', 'deflate', 'compress',):
            return None
        
        try:
            return getHTMLSerializer(result, pretty_print=False)
        except (TypeError, etree.ParseError):
            return None
    
    def transformString(self, result, encoding):
        return self.transformIterable([result], encoding)
    
    def transformUnicode(self, result, encoding):
        return self.transformIterable([result], encoding)
    
    def transformIterable(self, result, encoding):
        """Apply the transform if required
        """
        
        result = self.parseTree(result)
        if result is None:
            return None
        
        transform = self.setupTransform()
        if transform is None:
            return None
        
        # Find real or virtual path - PATH_INFO has VHM elements in it
        actualURL = self.request.get('ACTUAL_URL')
        
        siteURL = getSite().absolute_url()
        path = actualURL[len(siteURL):]
        
        # Find the host name
        base1 = self.request.get('BASE1')
        _, base1 = base1.split('://', 1)
        host = base1.lower()
        
        transformed = transform(result.tree, host='"%s"' % host, path='"%s"' % path)
        if transformed is None:
            return None
        
        result.tree = transformed
        
        return result

    def getSettings(self):
        return IThemeSettings(self.request)

class ThemeSettings(object):
    implements(IThemeSettings)
    adapts(IThemingLayer)

    def __init__(self, request):
        self.request = request
        base1 = request.get('BASE1')
        host = base1.lower()
        self.host = unicode(host)
        self.registry = queryUtility(IRegistry)
        self.themes = {}
        self.default = None
        try:
            self.default = self.registry.forInterface(IThemeSettings)
            registry = self.registry.forInterface(IDomainToTheme)
            domainsToTheme = registry.domainsToTheme
            if domainsToTheme:
                for d in domainsToTheme:
                    domain, theme = d.split('|')
                    domain, theme = domain.strip(), theme.strip()
                    self.themes[domain] = self.getThemeSettings(name=theme)
        except KeyError:
            pass
        #needed for cache
        self.__registry__ = None
        if self.default is not None:
            self.__registry__ = self.default.__registry__

    @property
    def enabled(self):
        settings = self.getThemeSettings()
        if settings is None:return False
        return settings.enabled

    @property
    def rules(self):
        settings = self.getThemeSettings()
        if settings is None:return u""
        return settings.rules

    @property
    def absolutePrefix(self):
        settings = self.getThemeSettings()
        if settings is None:return u""
        return settings.absolutePrefix

    @property
    def readNetwork(self):
        settings = self.getThemeSettings()
        if settings is None:return False
        return settings.readNetwork

    def getThemeSettings(self, name=None):
        """Return IThemeSettings object by theme name
        """
        if name is not None:
            return queryUtility(IThemeSettings, name, None)
        if self.host in self.themes.keys():
            return self.themes.get(self.host)
        return self.default

