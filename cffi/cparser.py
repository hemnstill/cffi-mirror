
from . import api, model
import pycparser, weakref

_parser_cache = None

def _get_parser():
    global _parser_cache
    if _parser_cache is None:
        _parser_cache = pycparser.CParser()
    return _parser_cache

class Parser(object):
    def __init__(self):
        self._declarations = {}
        self._anonymous_counter = 0
        self._structnode2type = weakref.WeakKeyDictionary()

    def _parse(self, csource):
        # XXX: for more efficiency we would need to poke into the
        # internals of CParser...  the following registers the
        # typedefs, because their presence or absence influences the
        # parsing itself (but what they are typedef'ed to plays no role)
        csourcelines = []
        for name in sorted(self._declarations):
            if name.startswith('typedef '):
                csourcelines.append('typedef int %s;' % (name[8:],))
        csourcelines.append('typedef int __dotdotdot__;')
        csourcelines.append(csource.replace('...', '__dotdotdot__'))
        csource = '\n'.join(csourcelines)
        ast = _get_parser().parse(csource)
        return ast

    def parse(self, csource):
        ast = self._parse(csource)
        # find the first "__dotdotdot__" and use that as a separator
        # between the repeated typedefs and the real csource
        iterator = iter(ast.ext)
        for decl in iterator:
            if decl.name == '__dotdotdot__':
                break
        #
        for decl in iterator:
            if isinstance(decl, pycparser.c_ast.Decl):
                self._parse_decl(decl)
            elif isinstance(decl, pycparser.c_ast.Typedef):
                if not decl.name:
                    raise api.CDefError("typedef does not declare any name",
                                        decl)
                self._declare('typedef ' + decl.name,
                              self._get_type(decl.type))
            else:
                raise api.CDefError("unrecognized construct", decl)

    def _parse_decl(self, decl):
        node = decl.type
        if isinstance(node, pycparser.c_ast.FuncDecl):
            self._declare('function ' + decl.name,
                          self._get_type(node, name=decl.name))
        else:
            if isinstance(node, pycparser.c_ast.Struct):
                # XXX do we need self._declare in any of those?
                if node.decls is not None:
                    self._get_struct_or_union_type('struct', node)
            elif isinstance(node, pycparser.c_ast.Union):
                if node.decls is not None:
                    self._get_struct_or_union_type('union', node)
            elif isinstance(node, pycparser.c_ast.Enum):
                if node.values is not None:
                    self._get_enum_type(node)
            elif not decl.name:
                raise api.CDefError("construct does not declare any variable",
                                    decl)
            #
            if decl.name:
                self._declare('variable ' + decl.name, self._get_type(node))

    def parse_type(self, cdecl, force_pointer=False):
        ast = self._parse('void __dummy(%s);' % cdecl)
        typenode = ast.ext[-1].type.args.params[0].type
        return self._get_type(typenode, force_pointer=force_pointer)

    def _declare(self, name, obj):
        if name in self._declarations:
            raise api.FFIError("multiple declarations of %s" % (name,))
        self._declarations[name] = obj

    def _get_type_pointer(self, type):
        if isinstance(type, model.FunctionType):
            return type # "pointer-to-function" ~== "function"
        return model.PointerType(type)

    def _get_type(self, typenode, convert_array_to_pointer=False,
                  force_pointer=False, name=None):
        # first, dereference typedefs, if we have it already parsed, we're good
        if (isinstance(typenode, pycparser.c_ast.TypeDecl) and
            isinstance(typenode.type, pycparser.c_ast.IdentifierType) and
            len(typenode.type.names) == 1 and
            ('typedef ' + typenode.type.names[0]) in self._declarations):
            type = self._declarations['typedef ' + typenode.type.names[0]]
            if isinstance(type, model.ArrayType):
                if convert_array_to_pointer:
                    return type.item
            else:
                if force_pointer:
                    return self._get_type_pointer(type)
            return type
        #
        if isinstance(typenode, pycparser.c_ast.ArrayDecl):
            # array type
            if convert_array_to_pointer:
                return self._get_type_pointer(self._get_type(typenode.type))
            if typenode.dim is None:
                length = None
            else:
                length = self._parse_constant(typenode.dim)
            return model.ArrayType(self._get_type(typenode.type), length)
        #
        if force_pointer:
            return model.PointerType(self._get_type(typenode))
        #
        if isinstance(typenode, pycparser.c_ast.PtrDecl):
            # pointer type
            return self._get_type_pointer(self._get_type(typenode.type))
        #
        if isinstance(typenode, pycparser.c_ast.TypeDecl):
            type = typenode.type
            if isinstance(type, pycparser.c_ast.IdentifierType):
                # assume a primitive type.  get it from .names, but reduce
                # synonyms to a single chosen combination
                names = list(type.names)
                if names == ['signed'] or names == ['unsigned']:
                    names.append('int')
                if names[0] == 'signed' and names != ['signed', 'char']:
                    names.pop(0)
                if (len(names) > 1 and names[-1] == 'int'
                        and names != ['unsigned', 'int']):
                    names.pop()
                ident = ' '.join(names)
                if ident == 'void':
                    return model.void_type
                return model.PrimitiveType(ident)
            #
            if isinstance(type, pycparser.c_ast.Struct):
                # 'struct foobar'
                return self._get_struct_or_union_type('struct', type, typenode)
            #
            if isinstance(type, pycparser.c_ast.Union):
                # 'union foobar'
                return self._get_struct_or_union_type('union', type, typenode)
            #
            if isinstance(type, pycparser.c_ast.Enum):
                # 'enum foobar'
                return self._get_enum_type(type)
        #
        if isinstance(typenode, pycparser.c_ast.FuncDecl):
            # a function type
            return self._parse_function_type(typenode, name)
        #
        raise api.FFIError("bad or unsupported type declaration")

    def _parse_function_type(self, typenode, funcname=None):
        params = list(getattr(typenode.args, 'params', []))
        ellipsis = (
            len(params) > 0 and
            isinstance(params[-1].type, pycparser.c_ast.TypeDecl) and
            isinstance(params[-1].type.type,
                       pycparser.c_ast.IdentifierType) and
            params[-1].type.type.names == ['__dotdotdot__'])
        if ellipsis:
            params.pop()
        if (len(params) == 1 and
            isinstance(params[0].type, pycparser.c_ast.TypeDecl) and
            isinstance(params[0].type.type, pycparser.c_ast.IdentifierType)
                and list(params[0].type.type.names) == ['void']):
            del params[0]
        args = [self._get_type(argdeclnode.type,
                               convert_array_to_pointer=True)
                for argdeclnode in params]
        result = self._get_type(typenode.type)
        return model.FunctionType(tuple(args), result, ellipsis)

    def _get_struct_or_union_type(self, kind, type, typenode=None):
        # First, a level of caching on the exact 'type' node of the AST.
        # This is obscure, but needed because pycparser "unrolls" declarations
        # such as "typedef struct { } foo_t, *foo_p" and we end up with
        # an AST that is not a tree, but a DAG, with the "type" node of the
        # two branches foo_t and foo_p of the trees being the same node.
        # It's a bit silly but detecting "DAG-ness" in the AST tree seems
        # to be the only way to distinguish this case from two independent
        # structs.  See test_struct_with_two_usages.
        try:
            return self._structnode2type[type]
        except KeyError:
            pass
        #
        # Note that this must handle parsing "struct foo" any number of
        # times and always return the same StructType object.  Additionally,
        # one of these times (not necessarily the first), the fields of
        # the struct can be specified with "struct foo { ...fields... }".
        # If no name is given, then we have to create a new anonymous struct
        # with no caching; in this case, the fields are either specified
        # right now or never.
        #
        name = type.name
        #
        # get the type or create it if needed
        if name is None:
            # 'typenode' is only used to guess a more readable name for
            # anonymous structs, for the common case "typedef struct { } foo".
            if typenode is not None and isinstance(typenode.declname, str):
                explicit_name = '$%s' % typenode.declname
            else:
                self._anonymous_counter += 1
                explicit_name = '$%d' % self._anonymous_counter
            tp = None
        else:
            explicit_name = name
            key = '%s %s' % (kind, name)
            tp = self._declarations.get(key, None)
        #
        if tp is None:
            if kind == 'struct':
                tp = model.StructType(explicit_name, None, None, None)
            elif kind == 'union':
                tp = model.UnionType(explicit_name, None, None, None)
            else:
                raise AssertionError("kind = %r" % (kind,))
            if name is not None:
                self._declare(key, tp)
        #
        self._structnode2type[type] = tp
        #
        # is there a 'type.decls'?  If yes, then this is the place in the
        # C sources that declare the fields.  If no, then just return the
        # existing type, possibly still incomplete.
        if type.decls is None:
            return tp
        #
        if tp.fldnames is not None:
            raise api.CDefError("duplicate declaration of struct %s" % name)
        fldnames = []
        fldtypes = []
        fldbitsize = []
        for decl in type.decls:
            if (isinstance(decl.type, pycparser.c_ast.IdentifierType) and
                    ''.join(decl.type.names) == '__dotdotdot__'):
                xxxx
                # XXX pycparser is inconsistent: 'names' should be a list
                # of strings, but is sometimes just one string.  Use
                # str.join() as a way to cope with both.
            if decl.bitsize is None:
                bitsize = -1
            else:
                bitsize = self._parse_constant(decl.bitsize)
            fldnames.append(decl.name)
            fldtypes.append(self._get_type(decl.type))
            fldbitsize.append(bitsize)
        tp.fldnames = tuple(fldnames)
        tp.fldtypes = tuple(fldtypes)
        tp.fldbitsize = tuple(fldbitsize)
        return tp

    def _parse_constant(self, exprnode):
        # for now, limited to expressions that are an immediate number
        # or negative number
        if isinstance(exprnode, pycparser.c_ast.Constant):
            return int(exprnode.value)
        #
        if (isinstance(exprnode, pycparser.c_ast.UnaryOp) and
                exprnode.op == '-'):
            return -self._parse_constant(exprnode.expr)
        #
        raise api.FFIError("unsupported non-constant or "
                           "not immediately constant expression")

    def _get_enum_type(self, type):
        name = type.name
        decls = type.values
        key = 'enum %s' % (name,)
        if key in self._declarations:
            return self._declarations[key]
        if decls is not None:
            enumerators = tuple([enum.name for enum in decls.enumerators])
            enumvalues = []
            nextenumvalue = 0
            for enum in decls.enumerators:
                if enum.value is not None:
                    nextenumvalue = self._parse_constant(enum.value)
                enumvalues.append(nextenumvalue)
                nextenumvalue += 1
            enumvalues = tuple(enumvalues) 
            tp = model.EnumType(name, enumerators, enumvalues)
            self._declarations[key] = tp
        else:   # opaque enum
            enumerators = ()
            enumvalues = ()
            tp = model.EnumType(name, (), ())
        return tp
