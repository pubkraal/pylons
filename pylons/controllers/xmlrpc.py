"""The base WSGI XMLRPCController"""
import inspect
import logging
import sys
import xmlrpclib

from paste.response import replace_header
from paste.wsgiwrappers import WSGIResponse

from pylons.controllers import WSGIController
from pylons.controllers.util import abort

__all__ = ['XMLRPCController']

log = logging.getLogger(__name__)

XMLRPC_MAPPING = ((basestring, 'string'), (list, 'array'), (bool, 'boolean'),
                  (int, 'int'), (float, 'double'), (dict, 'struct'), 
                  (xmlrpclib.DateTime, 'dateTime.iso8601'),
                  (xmlrpclib.Binary, 'base64'))

def xmlrpc_sig(args):
    """Returns a list of the function signature in string format based on a 
    tuple provided by xmlrpclib."""
    signature = []
    for param in args:
        for type, xml_name in XMLRPC_MAPPING:
            if isinstance(param, type):
                signature.append(xml_name)
                break
    return signature


def xmlrpc_fault(code, message):
    """Convienence method to return a Pylons response XMLRPC Fault"""
    fault = xmlrpclib.Fault(code, message)
    return WSGIResponse(xmlrpclib.dumps(fault, methodresponse=True))


def trim(docstring):
    """Yanked from PEP 237, strips the whitespace from Python doc strings"""
    if not docstring:
        return ''
    # Convert tabs to spaces (following the normal Python rules)
    # and split into a list of lines:
    lines = docstring.expandtabs().splitlines()
    # Determine minimum indentation (first line doesn't count):
    indent = sys.maxint
    for line in lines[1:]:
        stripped = line.lstrip()
        if stripped:
            indent = min(indent, len(line) - len(stripped))
    # Remove indentation (first line is special):
    trimmed = [lines[0].strip()]
    if indent < sys.maxint:
        for line in lines[1:]:
            trimmed.append(line[indent:].rstrip())
    # Strip off trailing and leading blank lines:
    while trimmed and not trimmed[-1]:
        trimmed.pop()
    while trimmed and not trimmed[0]:
        trimmed.pop(0)
    # Return a single string:
    return '\n'.join(trimmed)


class XMLRPCController(WSGIController):
    """XML-RPC Controller that speaks WSGI
    
    This controller handles XML-RPC responses and complies with the 
    `XML-RPC Specification <http://www.xmlrpc.com/spec>`_ as well as the
    `XML-RPC Introspection <http://scripts.incutio.com/xmlrpc/introspection.html>`_
    specification.
    
    By default, methods with names containing a dot are translated to use an
    underscore. For example, the `system.methodHelp` is handled by the method 
    `system_methodHelp`.
    
    Methods in the XML-RPC controller will be called with the method given in 
    the XMLRPC body. Methods may be annotated with a signature attribute to 
    declare the valid arguments and return types.
    
    For example::
        
        class MyXML(XMLRPCController):
            def userstatus(self):
                return 'basic string'
            userstatus.signature = [ ['string'] ]
            
            def userinfo(self, username, age=None):
                user = LookUpUser(username)
                response = {'username':user.name}
                if age and age > 10:
                    response['age'] = age
                return response
            userinfo.signature = [ ['struct', 'string'], ['struct', 'string', 'int'] ]
    
    Since XML-RPC methods can take different sets of data, each set of valid
    arguments is its own list. The first value in the list is the type of the
    return argument. The rest of the arguments are the types of the data that
    must be passed in.
    
    In the last method in the example above, since the method can optionally 
    take an integer value both sets of valid parameter lists should be
    provided.
    
    Valid types that can be checked in the signature and their corresponding
    Python types::

        'string' - str
        'array' - list
        'boolean' - bool
        'int' - int
        'double' - float
        'struct' - dict
        'dateTime.iso8601' - xmlrpclib.DateTime
        'base64' - xmlrpclib.Binary
    
    The class variable ``allow_none`` is passed to xmlrpclib.dumps; enabling it
    allows translating ``None`` to XML (an extension to the XML-RPC
    specification)

    Note::

        Requiring a signature is optional.
    """
    allow_none = False
    max_body_length = 4194304

    def _get_method_args(self):
        return self.rpc_kargs

    def __call__(self, environ, start_response):
        """Parse an XMLRPC body for the method, and call it with the 
        appropriate arguments"""
        # Pull out the length, return an error if there is no valid
        # length or if the length is larger than the max_body_length.
        length = environ.get('CONTENT_LENGTH')
        if length:
            length = int(length)
        else:
            # No valid Content-Length header found
            log.debug("No Content-Length found, returning 411 error")
            abort(411)
        if length > self.max_body_length or length == 0:
            log.debug("Content-Length larger than max body length. Max: %s,"
                      " Sent: %s. Returning 413 error", self.max_body_length, 
                      length)
            abort(413, "XML body too large")

        body = environ['wsgi.input'].read(int(environ['CONTENT_LENGTH']))
        rpc_args, orig_method = xmlrpclib.loads(body)

        method = self._find_method_name(orig_method)
        log.debug("Looking for XMLRPC method called: %s", method)
        try:
            has_method = hasattr(self, method)
        except UnicodeEncodeError:
            has_method = False
        if not has_method:
            log.debug("No method found, returning xmlrpc fault")
            return xmlrpc_fault(0, "No method by that name")(environ, start_response)

        func = getattr(self, method)

        # Signature checking for params
        if hasattr(func, 'signature'):
            log.debug("Checking XMLRPC argument signature")
            valid_args = False
            params = xmlrpc_sig(rpc_args)
            for sig in func.signature:
                # Next sig if we don't have the same amount of args
                if len(sig)-1 != len(rpc_args):
                    continue

                # If the params match, we're valid
                if params == sig[1:]:
                    valid_args = True
                    break

            if not valid_args:
                log.debug("Bad argument signature recieved, returning xmlrpc"
                          " fault")
                msg = ("Incorrect argument signature. %s recieved does not "
                       "match %s signature for method %s" % \
                           (params, func.signature, orig_method))
                return xmlrpc_fault(0, msg)(environ, start_response)

        # Change the arg list into a keyword dict based off the arg
        # names in the functions definition
        arglist = inspect.getargspec(func)[0][1:]
        kargs = dict(zip(arglist, rpc_args))
        kargs['action'], kargs['environ'] = method, environ
        kargs['start_response'] = start_response
        self.rpc_kargs = kargs
        self._func = func
        
        # Now that we know the method is valid, and the args are valid,
        # we can dispatch control to the default WSGIController
        status = []
        headers = []
        exc_info = []
        def change_content(new_status, new_headers, new_exc_info=None):
            status.append(new_status)
            headers.extend(new_headers)
            exc_info.append(new_exc_info)
        output = WSGIController.__call__(self, environ, change_content)
        replace_header(headers, 'Content-Type', 'text/xml')
        start_response(status[0], headers, exc_info[0])
        return output

    def _dispatch_call(self):
        """Dispatch the call to the function chosen by __call__"""
        raw_response = self._inspect_call(self._func)
        if not isinstance(raw_response, xmlrpclib.Fault):
            raw_response = (raw_response,)

        response = xmlrpclib.dumps(raw_response, methodresponse=True,
                                   allow_none=self.allow_none)
        return WSGIResponse(response)

    def _find_method_name(self, name):
        """Locate a method in the controller by the appropriate name
        
        By default, this translates method names like 'system.methodHelp' into
        'system_methodHelp'.
        """
        return name.replace('.', '_')

    def _publish_method_name(self, name):
        """Translate an internal method name to a publicly viewable one
        
        By default, this translates internal method names like 'blog_view' into
        'blog.view'.
        """
        return name.replace('_', '.')

    def system_listMethods(self):
        """Returns a list of XML-RPC methods for this XML-RPC resource"""
        methods = []
        for method in dir(self):
            meth = getattr(self, method)

            # Only methods have this attribute
            if not method.startswith('_') and hasattr(meth, 'im_self'):
                methods.append(self._publish_method_name(method))
        return methods
    system_listMethods.signature = [['array']]

    def system_methodSignature(self, name):
        """Returns an array of array's for the valid signatures for a method.

        The first value of each array is the return value of the method. The
        result is an array to indicate multiple signatures a method may be
        capable of.
        """
        name = self._find_method_name(name)
        if hasattr(self, name):
            method = getattr(self, name)
            if hasattr(method, 'signature'):
                return getattr(method, 'signature')
            else:
                return ''
        else:
            return xmlrpclib.Fault(0, 'No such method name')
    system_methodSignature.signature = [['array', 'string'],
                                        ['string', 'string']]

    def system_methodHelp(self, name):
        """Returns the documentation for a method"""
        name = self._find_method_name(name)
        if hasattr(self, name):
            method = getattr(self, name)
            help = getattr(method, 'help', None) or method.__doc__
            help = trim(help)
            sig = getattr(method, 'signature', None)
            if sig:
                help += "\n\nMethod signature: %s" % sig
            return help
        return xmlrpclib.Fault(0, "No such method name")
    system_methodHelp.signature = [['string', 'string']]
