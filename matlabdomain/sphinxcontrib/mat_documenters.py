# -*- coding: utf-8 -*-
"""
    sphinxcontrib.mat_documenters
    ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

    Extend autodoc directives to matlabdomain.

    :copyright: Copyright 2014 Mark Mikofski
    :license: BSD, see LICENSE for details.
"""

import re
import sys
import inspect
import traceback
from types import FunctionType, BuiltinFunctionType, MethodType

from docutils import nodes
from docutils.utils import assemble_option_dict
from docutils.statemachine import ViewList

from sphinx.util import rpartition, force_decode
from sphinx.locale import _
from sphinx.pycode import ModuleAnalyzer as PyModuleAnalyzer, PycodeError
from sphinx.application import ExtensionError
from sphinx.util.nodes import nested_parse_with_titles
from sphinx.util.compat import Directive
from sphinx.util.inspect import getargspec, isdescriptor, safe_getmembers, \
     safe_getattr, safe_repr, is_builtin_class_method
from sphinx.util.pycompat import base_exception, class_types
from sphinx.util.docstrings import prepare_docstring

from sphinx.ext.autodoc import py_ext_sig_re as mat_ext_sig_re, \
    DefDict, identity, Options, ALL, INSTANCEATTR, members_option, \
    members_set_option, SUPPRESS, annotation_option, bool_option, \
    Documenter as PyDocumenter, \
    ModuleDocumenter as PyModuleDocumenter, \
    FunctionDocumenter as PyFunctionDocumenter, \
    ClassDocumenter as PyClassDocumenter, \
    ExceptionDocumenter as PyExceptionDocumenter, \
    DataDocumenter as PyDataDocumenter, \
    MethodDocumenter as PyMethodDocumenter, \
    AutoDirective

from mat_types import MatObject, MatModule, MatFunction, MatClass, \
    MatProperty, MatMethod, MatScript, MatException, MatModuleAnalyzer

# TODO: check MRO's for all classes, attributes and methods!!!


class MatlabDocumenter(PyDocumenter):
    """
    Base class for documenters of MATLAB objects.
    """
    domain = 'mat'

    def import_object(self):
        """Import the object given by *self.modname* and *self.objpath* and set
        it as *self.object*.

        Returns True if successful, False if an error occurred.
        """
        # get config_value with absolute path to MATLAB source files
        basedir = self.env.config.matlab_src_dir
        dbg = self.env.app.debug
        MatObject.basedir = basedir  # set MatObject base directory
        MatObject.sphinx_env = self.env  # pass env to MatObject cls
        MatObject.sphinx_app = self.env.app  # pass app to MatObject cls
        MatObject.sphinx_dbg = dbg  # pass dbg to MatObject cls
        if self.objpath:
            dbg('[autodoc] from %s import %s',
                self.modname, '.'.join(self.objpath))
        try:
            dbg('[autodoc] import %s', self.modname)
            MatObject.matlabify(self.modname)
            parent = None
            obj = self.module = sys.modules[self.modname]
            dbg('[autodoc] => %r', obj)
            for part in self.objpath:
                parent = obj
                dbg('[autodoc] getattr(_, %r)', part)
                obj = self.get_attr(obj, part)
                dbg('[autodoc] => %r', obj)
                self.object_name = part
            self.parent = parent
            self.object = obj
            return True
        # this used to only catch SyntaxError, ImportError and AttributeError,
        # but importing modules with side effects can raise all kinds of errors
        except Exception:
            if self.objpath:
                errmsg = 'autodoc: failed to import %s %r from module %r' % \
                         (self.objtype, '.'.join(self.objpath), self.modname)
            else:
                errmsg = 'autodoc: failed to import %s %r' % \
                         (self.objtype, self.fullname)
            errmsg += '; the following exception was raised:\n%s' % \
                      traceback.format_exc()
            dbg(errmsg)
            self.directive.warn(errmsg)
            self.env.note_reread()
            return False

    def add_content(self, more_content, no_docstring=False):
        """Add content from docstrings, attribute documentation and user."""
        # set sourcename and add content from attribute documentation
        if self.analyzer:
            # prevent encoding errors when the file name is non-ASCII
            if not isinstance(self.analyzer.srcname, unicode):
                filename = unicode(self.analyzer.srcname,
                                   sys.getfilesystemencoding(), 'replace')
            else:
                filename = self.analyzer.srcname
            sourcename = u'%s:docstring of %s' % (filename, self.fullname)

            attr_docs = self.analyzer.find_attr_docs()
            if self.objpath:
                key = ('.'.join(self.objpath[:-1]), self.objpath[-1])
                if key in attr_docs:
                    no_docstring = True
                    docstrings = [attr_docs[key]]
                    for i, line in enumerate(self.process_doc(docstrings)):
                        self.add_line(line, sourcename, i)
        else:
            sourcename = u'docstring of %s' % self.fullname

        # add content from docstrings
        if not no_docstring:
            encoding = self.analyzer and self.analyzer.encoding
            docstrings = self.get_doc(encoding)
            if not docstrings:
                # append at least a dummy docstring, so that the event
                # autodoc-process-docstring is fired and can add some
                # content if desired
                docstrings.append([])
            for i, line in enumerate(self.process_doc(docstrings)):
                self.add_line(line, sourcename, i)

        # add additional content (e.g. from document), if present
        if more_content:
            for line, src in zip(more_content.data, more_content.items):
                self.add_line(line, src[0], src[1])

    def get_object_members(self, want_all):
        """Return `(members_check_module, members)` where `members` is a
        list of `(membername, member)` pairs of the members of *self.object*.

        If *want_all* is True, return all members.  Else, only return those
        members given by *self.options.members* (which may also be none).
        """
        analyzed_member_names = set()
        if self.analyzer:
            attr_docs = self.analyzer.find_attr_docs()
            namespace = '.'.join(self.objpath)
            for item in attr_docs.iteritems():
                if item[0][0] == namespace:
                    analyzed_member_names.add(item[0][1])
        if not want_all:
            if not self.options.members:
                return False, []
            # specific members given
            members = []
            for mname in self.options.members:
                try:
                    members.append((mname, self.get_attr(self.object, mname)))
                except AttributeError:
                    if mname not in analyzed_member_names:
                        self.directive.warn('missing attribute %s in object %s'
                                            % (mname, self.fullname))
        elif self.options.inherited_members:
            # safe_getmembers() uses dir() which pulls in members from all
            # base classes
            members = safe_getmembers(self.object, attr_getter=self.get_attr)
        else:
            # __dict__ contains only the members directly defined in
            # the class (but get them via getattr anyway, to e.g. get
            # unbound method objects instead of function objects);
            # using keys() because apparently there are objects for which
            # __dict__ changes while getting attributes
            try:
                obj_dict = self.get_attr(self.object, '__dict__')
            except AttributeError:
                members = []
            else:
                members = [(mname, self.get_attr(self.object, mname, None))
                           for mname in obj_dict.keys()]
        membernames = set(m[0] for m in members)
        # add instance attributes from the analyzer
        for aname in analyzed_member_names:
            if aname not in membernames and \
               (want_all or aname in self.options.members):
                members.append((aname, INSTANCEATTR))
        return False, sorted(members)

    def filter_members(self, members, want_all):
        """Filter the given member list.

        Members are skipped if

        - they are private (except if given explicitly or the private-members
          option is set)
        - they are special methods (except if given explicitly or the
          special-members option is set)
        - they are undocumented (except if the undoc-members option is set)

        The user can override the skipping decision by connecting to the
        ``autodoc-skip-member`` event.
        """
        ret = []

        # search for members in source code too
        namespace = '.'.join(self.objpath)  # will be empty for modules

        if self.analyzer:
            attr_docs = self.analyzer.find_attr_docs()
        else:
            attr_docs = {}

        # process members and determine which to skip
        for (membername, member) in members:
            # if isattr is True, the member is documented as an attribute
            isattr = False

            doc = self.get_attr(member, '__doc__', None)
            # if the member __doc__ is the same as self's __doc__, it's just
            # inherited and therefore not the member's doc
            cls = self.get_attr(member, '__class__', None)
            if cls:
                cls_doc = self.get_attr(cls, '__doc__', None)
                if cls_doc == doc:
                    doc = None
            has_doc = bool(doc)

            keep = False
            if want_all and membername.startswith('__') and \
                   membername.endswith('__') and len(membername) > 4:
                # special __methods__
                if self.options.special_members is ALL and \
                        membername != '__doc__':
                    keep = has_doc or self.options.undoc_members
                elif self.options.special_members and \
                     self.options.special_members is not ALL and \
                        membername in self.options.special_members:
                    keep = has_doc or self.options.undoc_members
            elif want_all and membername.startswith('_'):
                # ignore members whose name starts with _ by default
                keep = self.options.private_members and \
                       (has_doc or self.options.undoc_members)
            elif (namespace, membername) in attr_docs:
                # keep documented attributes
                keep = True
                isattr = True
            else:
                # ignore undocumented members if :undoc-members: is not given
                keep = has_doc or self.options.undoc_members

            # give the user a chance to decide whether this member
            # should be skipped
            if self.env.app:
                # let extensions preprocess docstrings
                skip_user = self.env.app.emit_firstresult(
                    'autodoc-skip-member', self.objtype, membername, member,
                    not keep, self.options)
                if skip_user is not None:
                    keep = not skip_user

            if keep:
                ret.append((membername, member, isattr))

        return ret

    def document_members(self, all_members=False):
        """Generate reST for member documentation.

        If *all_members* is True, do all members, else those given by
        *self.options.members*.
        """
        # set current namespace for finding members
        self.env.temp_data['autodoc:module'] = self.modname
        if self.objpath:
            self.env.temp_data['autodoc:class'] = self.objpath[0]

        want_all = all_members or self.options.inherited_members or \
                   self.options.members is ALL
        # find out which members are documentable
        members_check_module, members = self.get_object_members(want_all)

        # remove members given by exclude-members
        if self.options.exclude_members:
            members = [(membername, member) for (membername, member) in members
                       if membername not in self.options.exclude_members]

        # document non-skipped members
        memberdocumenters = []
        for (mname, member, isattr) in self.filter_members(members, want_all):
            classes = [cls for cls in AutoDirective._registry.itervalues()
                       if cls.can_document_member(member, mname, isattr, self)]
            if not classes:
                # don't know how to document this member
                continue
            # prefer the documenter with the highest priority
            classes.sort(key=lambda cls: cls.priority)
            # give explicitly separated module name, so that members
            # of inner classes can be documented
            full_mname = self.modname + '::' + \
                              '.'.join(self.objpath + [mname])
            documenter = classes[-1](self.directive, full_mname, self.indent)
            memberdocumenters.append((documenter, isattr))
        member_order = self.options.member_order or \
                       self.env.config.autodoc_member_order
        if member_order == 'groupwise':
            # sort by group; relies on stable sort to keep items in the
            # same group sorted alphabetically
            memberdocumenters.sort(key=lambda e: e[0].member_order)
        elif member_order == 'bysource' and self.analyzer:
            # sort by source order, by virtue of the module analyzer
            tagorder = self.analyzer.tagorder
            def keyfunc(entry):
                fullname = entry[0].name.split('::')[1]
                return tagorder.get(fullname, len(tagorder))
            memberdocumenters.sort(key=keyfunc)

        for documenter, isattr in memberdocumenters:
            documenter.generate(
                all_members=True, real_modname=self.real_modname,
                check_module=members_check_module and not isattr)

        # reset current objects
        self.env.temp_data['autodoc:module'] = None
        self.env.temp_data['autodoc:class'] = None

    def generate(self, more_content=None, real_modname=None,
                 check_module=False, all_members=False):
        """Generate reST for the object given by *self.name*, and possibly for
        its members.

        If *more_content* is given, include that content. If *real_modname* is
        given, use that module name to find attribute docs. If *check_module* is
        True, only generate if the object is defined in the module name it is
        imported from. If *all_members* is True, document all members.
        """
        if not self.parse_name():
            # need a module to import
            self.directive.warn(
                'don\'t know which module to import for autodocumenting '
                '%r (try placing a "module" or "currentmodule" directive '
                'in the document, or giving an explicit module name)'
                % self.name)
            return

        # now, import the module and get object to document
        if not self.import_object():
            return

        # If there is no real module defined, figure out which to use.
        # The real module is used in the module analyzer to look up the module
        # where the attribute documentation would actually be found in.
        # This is used for situations where you have a module that collects the
        # functions and classes of internal submodules.
        self.real_modname = real_modname or self.get_real_modname()

        # try to also get a source code analyzer for attribute docs
        try:
            self.analyzer = MatModuleAnalyzer.for_module(self.real_modname)
            # parse right now, to get PycodeErrors on parsing (results will
            # be cached anyway)
            self.analyzer.find_attr_docs()
        except PycodeError, err:
            self.env.app.debug('[autodoc] module analyzer failed: %s', err)
            # no source file -- e.g. for builtin and C modules
            self.analyzer = None
            # at least add the module.__file__ as a dependency
            if hasattr(self.module, '__file__') and self.module.__file__:
                self.directive.filename_set.add(self.module.__file__)
        else:
            self.directive.filename_set.add(self.analyzer.srcname)

        # check __module__ of object (for members not given explicitly)
        if check_module:
            if not self.check_module():
                return

        # make sure that the result starts with an empty line.  This is
        # necessary for some situations where another directive preprocesses
        # reST and no starting newline is present
        self.add_line(u'', '<autodoc>')

        # format the object's signature, if any
        sig = self.format_signature()

        # generate the directive header and options, if applicable
        self.add_directive_header(sig)
        self.add_line(u'', '<autodoc>')

        # e.g. the module directive doesn't have content
        self.indent += self.content_indent

        # add all content (from docstrings, attribute docs etc.)
        self.add_content(more_content)

        # document members, if possible
        self.document_members(all_members)


class MatModuleDocumenter(MatlabDocumenter, PyModuleDocumenter):

    def parse_name(self):
        ret = MatlabDocumenter.parse_name(self)
        if self.args or self.retann:
            self.directive.warn('signature arguments or return annotation '
                                'given for automodule %s' % self.fullname)
        return ret

    def add_directive_header(self, sig):
        MatlabDocumenter.add_directive_header(self, sig)

        # add some module-specific options
        if self.options.synopsis:
            self.add_line(
                u'   :synopsis: ' + self.options.synopsis, '<autodoc>')
        if self.options.platform:
            self.add_line(
                u'   :platform: ' + self.options.platform, '<autodoc>')
        if self.options.deprecated:
            self.add_line(u'   :deprecated:', '<autodoc>')

    def get_object_members(self, want_all):
        if want_all:
            if not hasattr(self.object, '__all__'):
                # for implicit module members, check __module__ to avoid
                # documenting imported objects
                return True, self.object.safe_getmembers()
            else:
                memberlist = self.object.__all__
        else:
            memberlist = self.options.members or []
        ret = []
        for mname in memberlist:
            try:
                attr = self.get_attr(self.object, mname, None)
                if attr:
                    ret.append((mname, attr))
                else:
                    raise AttributeError
            except AttributeError:
                self.directive.warn(
                    'missing attribute mentioned in :members: or __all__: '
                    'module %s, attribute %s' % (
                    safe_getattr(self.object, '__name__', '???'), mname))
        return False, ret


class MatModuleLevelDocumenter(MatlabDocumenter):
    """
    Specialized Documenter subclass for objects on module level (functions,
    classes, data/constants).
    """
    def resolve_name(self, modname, parents, path, base):
        if modname is None:
            if path:
                modname = path.rstrip('.')
            else:
                # if documenting a toplevel object without explicit module,
                # it can be contained in another auto directive ...
                modname = self.env.temp_data.get('autodoc:module')
                # ... or in the scope of a module directive
                if not modname:
                    modname = self.env.temp_data.get('mat:module')
                # ... else, it stays None, which means invalid
        return modname, parents + [base]


class MatClassLevelDocumenter(MatlabDocumenter):
    """
    Specialized Documenter subclass for objects on class level (methods,
    attributes).
    """
    def resolve_name(self, modname, parents, path, base):
        if modname is None:
            if path:
                mod_cls = path.rstrip('.')
            else:
                mod_cls = None
                # if documenting a class-level object without path,
                # there must be a current class, either from a parent
                # auto directive ...
                mod_cls = self.env.temp_data.get('autodoc:class')
                # ... or from a class directive
                if mod_cls is None:
                    mod_cls = self.env.temp_data.get('mat:class')
                # ... if still None, there's no way to know
                if mod_cls is None:
                    return None, []
            modname, cls = rpartition(mod_cls, '.')
            parents = [cls]
            # if the module name is still missing, get it like above
            if not modname:
                modname = self.env.temp_data.get('autodoc:module')
            if not modname:
                modname = self.env.temp_data.get('mat:module')
            # ... else, it stays None, which means invalid
        return modname, parents + [base]


class MatDocstringSignatureMixin(object):
    """
    Mixin for FunctionDocumenter and MethodDocumenter to provide the
    feature of reading the signature from the docstring.
    """

    def _find_signature(self, encoding=None):
        docstrings = MatlabDocumenter.get_doc(self, encoding)
        if len(docstrings) != 1:
            return
        doclines = docstrings[0]
        setattr(self, '__new_doclines', doclines)
        if not doclines:
            return
        # match first line of docstring against signature RE
        match = mat_ext_sig_re.match(doclines[0])
        if not match:
            return
        exmod, path, base, args, retann = match.groups()
        # the base name must match ours
        if not self.objpath or base != self.objpath[-1]:
            return
        # re-prepare docstring to ignore indentation after signature
        docstrings = MatlabDocumenter.get_doc(self, encoding, 2)
        doclines = docstrings[0]
        # ok, now jump over remaining empty lines and set the remaining
        # lines as the new doclines
        i = 1
        while i < len(doclines) and not doclines[i].strip():
            i += 1
        setattr(self, '__new_doclines', doclines[i:])
        return args, retann

    def get_doc(self, encoding=None, ignore=1):
        lines = getattr(self, '__new_doclines', None)
        if lines is not None:
            return [lines]
        return MatlabDocumenter.get_doc(self, encoding, ignore)

    def format_signature(self):
        if self.args is None and self.env.config.autodoc_docstring_signature:
            # only act if a signature is not explicitly given already, and if
            # the feature is enabled
            result = self._find_signature()
            if result is not None:
                self.args, self.retann = result
        return MatlabDocumenter.format_signature(self)


class MatFunctionDocumenter(MatDocstringSignatureMixin,
                            MatModuleLevelDocumenter):
    """
    Specialized Documenter subclass for functions.
    """
    objtype = 'function'
    member_order = 30

    @classmethod
    def can_document_member(cls, member, membername, isattr, parent):
        return isinstance(member, MatFunction)

    def format_args(self):
        if self.object.args:
            argspec = inspect.ArgSpec(self.object.args, None, None, None)
        else:
            return None
        args = inspect.formatargspec(*argspec)
        # escape backslashes for reST
        args = args.replace('\\', '\\\\')
        return args

    def document_members(self, all_members=False):
        pass


class MatClassDocumenter(MatModuleLevelDocumenter):
    """
    Specialized Documenter subclass for classes.
    """
    objtype = 'class'
    member_order = 20
    option_spec = {
        'members': members_option, 'undoc-members': bool_option,
        'noindex': bool_option, 'inherited-members': bool_option,
        'show-inheritance': bool_option, 'member-order': identity,
        'exclude-members': members_set_option,
        'private-members': bool_option, 'special-members': members_option,
    }

    @classmethod
    def can_document_member(cls, member, membername, isattr, parent):
        return isinstance(member, MatClass)

    def import_object(self):
        ret = MatModuleLevelDocumenter.import_object(self)
        # if the class is documented under another name, document it
        # as data/attribute
        if ret:
            if hasattr(self.object, '__name__'):
                self.doc_as_attr = (self.objpath[-1] != self.object.__name__)
            else:
                self.doc_as_attr = True
        return ret

    def format_args(self):
        # for classes, the relevant signature is the "constructor" method,
        # which has the same name as the class definition
        initmeth = self.get_attr(self.object, self.object.name, None)
        # classes without constructor method, default constructor or
        # constructor written in C?
        if initmeth is None or not isinstance(initmeth, MatMethod):
            return None
        if initmeth.args:
            argspec = inspect.ArgSpec(initmeth.args, None, None, None)
        else:
            return None
        if argspec[0] and argspec[0][0] in ('obj', ):
            del argspec[0][0]
        return inspect.formatargspec(*argspec)

    def format_signature(self):
        if self.doc_as_attr:
            return ''

        # get constructor method signature from docstring
        if self.env.config.autodoc_docstring_signature:
            # only act if the feature is enabled
            init_doc = MatMethodDocumenter(self.directive, self.object.name)
            init_doc.object = self.get_attr(self.object, self.object.name, None)
            init_doc.objpath = [self.object.name]
            result = init_doc._find_signature()
            if result is not None:
                # use args only for Class signature
                return '(%s)' % result[0]

        return MatModuleLevelDocumenter.format_signature(self)

    def add_directive_header(self, sig):
        if self.doc_as_attr:
            self.directivetype = 'attribute'
        MatlabDocumenter.add_directive_header(self, sig)

        # add inheritance info, if wanted
        if not self.doc_as_attr and self.options.show_inheritance:
            self.add_line(u'', '<autodoc>')
            if len(self.object.__bases__):
                bases = [not v and u':class:`%s`' % b or
                         u':class:`%s.%s`' % (v.__module__, b)
                         for b, v in self.object.__bases__.iteritems()]
                self.add_line(_(u'   Bases: %s') % ', '.join(bases),
                              '<autodoc>')

    def get_doc(self, encoding=None, ignore=1):
        content = self.env.config.autoclass_content

        docstrings = []
        attrdocstring = self.get_attr(self.object, '__doc__', None)
        if attrdocstring:
            docstrings.append(attrdocstring)

        # for classes, what the "docstring" is can be controlled via a
        # config value; the default is only the class docstring
        if content in ('both', 'init'):
            # get __init__ method document from __init__.__doc__
            if self.env.config.autodoc_docstring_signature:
                # only act if the feature is enabled
                init_doc = MatMethodDocumenter(self.directive, self.object.name)
                init_doc.object = self.get_attr(self.object, self.object.name,
                                                None)
                init_doc.objpath = [self.object.name]
                init_doc._find_signature()  # this effects to get_doc() result
                initdocstring = '\n'.join(
                    ['\n'.join(l) for l in init_doc.get_doc(encoding)])
            else:
                initdocstring = self.get_attr(
                    self.get_attr(self.object, self.object.name, None),
                    '__doc__')
            # for new-style classes, no __init__ means default __init__
            if initdocstring == object.__init__.__doc__:
                initdocstring = None
            if initdocstring:
                if content == 'init':
                    docstrings = [initdocstring]
                else:
                    docstrings.append(initdocstring)
        doc = []
        for docstring in docstrings:
            if not isinstance(docstring, unicode):
                docstring = force_decode(docstring, encoding)
            doc.append(prepare_docstring(docstring))
        return doc

    def add_content(self, more_content, no_docstring=False):
        if self.doc_as_attr:
            classname = safe_getattr(self.object, '__name__', None)
            if classname:
                content = ViewList(
                    [_('alias of :class:`%s`') % classname], source='')
                MatModuleLevelDocumenter.add_content(self, content,
                                                  no_docstring=True)
        else:
            MatModuleLevelDocumenter.add_content(self, more_content)

    def document_members(self, all_members=False):
        if self.doc_as_attr:
            return
        MatModuleLevelDocumenter.document_members(self, all_members)


class MatExceptionDocumenter(MatlabDocumenter, PyExceptionDocumenter):

    @classmethod
    def can_document_member(cls, member, membername, isattr, parent):
        return isinstance(member, MatException)


class MatDataDocumenter(MatModuleLevelDocumenter, PyDataDocumenter):

    @classmethod
    def can_document_member(cls, member, membername, isattr, parent):
        return isinstance(member, MatScript)


class MatMethodDocumenter(MatDocstringSignatureMixin, MatClassLevelDocumenter):
    """
    Specialized Documenter subclass for methods (normal, static and class).
    """
    objtype = 'method'
    member_order = 50
    priority = 1  # must be more than FunctionDocumenter

    @classmethod
    def can_document_member(cls, member, membername, isattr, parent):
        return isinstance(member, MatMethod)

    def import_object(self):
        ret = MatClassLevelDocumenter.import_object(self)
        if self.object.attrs.get('Static'):
            self.directivetype = 'staticmethod'
            # document class and static members before ordinary ones
            self.member_order = self.member_order - 1
        else:
            self.directivetype = 'method'
        return ret

    def format_args(self):
        if self.object.args:
            argspec = inspect.ArgSpec(self.object.args, None, None, None)
        else:
            return None
        if argspec[0] and argspec[0][0] in ('obj', ):
            del argspec[0][0]
        return inspect.formatargspec(*argspec)

    def document_members(self, all_members=False):
        pass


class MatAttributeDocumenter(MatClassLevelDocumenter):
    """
    Specialized Documenter subclass for attributes.
    """
    objtype = 'attribute'
    member_order = 60
    option_spec = dict(MatModuleLevelDocumenter.option_spec)
    option_spec["annotation"] = annotation_option

    # must be higher than the MethodDocumenter, else it will recognize
    # some non-data descriptors as methods
    priority = 10

    @classmethod
    def can_document_member(cls, member, membername, isattr, parent):
        return isinstance(member, MatProperty) and member.attrs.get('Constant')

    def document_members(self, all_members=False):
        pass

    def import_object(self):
        ret = MatClassLevelDocumenter.import_object(self)
        # getset = self.object.name.split('_')
        if (#getset[0] in ['get','set'] and
            #getset[1:] in self.object.cls.propeties and
            isinstance(self.object, MatMethod)):
            self._datadescriptor = True
        else:
            # if it's not a data descriptor
            self._datadescriptor = False
        return ret

    def get_real_modname(self):
        return self.get_attr(self.parent or self.object, '__module__', None) \
               or self.modname

    def add_directive_header(self, sig):
        MatClassLevelDocumenter.add_directive_header(self, sig)
        if not self.options.annotation:
            if not self._datadescriptor:
                try:
                    objrepr = safe_repr(self.object.default)  # display default
                except ValueError:
                    pass
                else:
                    self.add_line(u'   :annotation: = ' + objrepr, '<autodoc>')
        elif self.options.annotation is SUPPRESS:
            pass
        else:
            self.add_line(u'   :annotation: %s' % self.options.annotation,
                          '<autodoc>')

    def add_content(self, more_content, no_docstring=False):
        # if not self._datadescriptor:
        #     # if it's not a data descriptor, its docstring is very probably the
        #     # wrong thing to display
        #     no_docstring = True
        MatClassLevelDocumenter.add_content(self, more_content, no_docstring)


class MatInstanceAttributeDocumenter(MatAttributeDocumenter):
    """
    Specialized Documenter subclass for attributes that cannot be imported
    because they are instance attributes (e.g. assigned in __init__).
    """
    objtype = 'instanceattribute'
    directivetype = 'attribute'
    member_order = 60

    # must be higher than AttributeDocumenter
    priority = 11

    # @classmethod
    # def can_document_member(cls, member, membername, isattr, parent):
    #     """This documents only INSTANCEATTR members."""
    #     return isattr and (member is INSTANCEATTR)

    @classmethod
    def can_document_member(cls, member, membername, isattr, parent):
        is_instance_attr = (isinstance(member, MatProperty) and
                            not member.attrs.get('Constant'))
        return is_instance_attr

    # def import_object(self):
    #     """Never import anything."""
    #     # disguise as an attribute
    #     self.objtype = 'attribute'
    #     self._datadescriptor = False
    #     return True

    def add_content(self, more_content, no_docstring=False):
        """Never try to get a docstring from the object."""
        # MatAttributeDocumenter.add_content(self, more_content,
        #                                    no_docstring=True)
        MatAttributeDocumenter.add_content(self, more_content, no_docstring)
