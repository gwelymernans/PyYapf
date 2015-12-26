# -*- coding: utf-8 -*-
"""
Sublime Text 3 Plugin to invoke Yapf on a python file.
"""
import codecs
import os
import subprocess
import sys
import tempfile
import configparser
import textwrap

import sublime, sublime_plugin

KEY = "pyyapf"


def save_style_to_tempfile(style):
    # build config object
    cfg = configparser.RawConfigParser()
    cfg.add_section('style')
    for key, value in style.items():
        cfg.set('style', key, value)

    # dump it to temporary file
    fobj, fname = tempfile.mkstemp()
    cfg.write(os.fdopen(fobj, "w"))
    return fname


def dedent_text(text):
    new_text = textwrap.dedent(text)

    # determine original indentation
    old_first = text.splitlines()[0]
    new_first = new_text.splitlines()[0]
    assert old_first.endswith(new_first), 'PyYapf: Dedent logic flawed'
    indent = old_first[:len(old_first) - len(new_first)]

    # determine if have trailing newline (when using the "yapf_selection"
    # command, it can happen that there is none)
    trailing_nl = text.endswith('\n')

    return new_text, indent, trailing_nl


def indent_text(text, indent, trailing_nl):
    # reindent
    text = textwrap.indent(text, indent)

    # remove trailing newline if so desired
    if not trailing_nl and text.endswith('\n'):
        text = text[:-1]

    return text


def is_python(view):
    return view.score_selector(0, 'source.python') > 0


class Yapf:
    """
    This class wraps YAPF invocation, including encoding/decoding and error handling.
    """

    def __init__(self, view):
        self.settings = sublime.load_settings("PyYapf.sublime-settings")
        self.view = view

        # determine encoding
        self.encoding = self.view.encoding()
        if self.encoding == 'Undefined':
            self.encoding = self.settings.get('default_encoding')
            self.debug('Encoding is not specified, falling back to default %r',
                       self.encoding)
        else:
            self.debug('Encoding is %r', self.encoding)

        # custom style options?
        custom_style = self.settings.get("config")
        if custom_style:
            # write style file to temporary file
            self.custom_style_fname = save_style_to_tempfile(custom_style)
            self.debug('Using custom style (%s):\n%s', self.custom_style_fname,
                       open(self.custom_style_fname).read().strip())
        else:
            self.custom_style_fname = None

        # prepare popen arguments
        cmd = self.settings.get("yapf_command")
        if not cmd:
            msg = 'Yapf command not configured. Problem with settings?'
            sublime.error_message(msg)
            raise Exception(msg)
        cmd = os.path.expanduser(cmd)

        self.popen_args = [cmd, '--verify']
        if self.custom_style_fname:
            self.popen_args += ['--style', self.custom_style_fname]

        # use directory of current file so that custom styles are found properly
        fname = self.view.file_name()
        self.popen_cwd = os.path.dirname(fname) if fname else None

        # specify encoding in environment
        self.popen_env = os.environ.copy()
        self.popen_env['LANG'] = self.encoding

        # win32: hide console window
        if sys.platform in ('win32', 'cygwin'):
            self.popen_startupinfo = subprocess.STARTUPINFO()
            self.popen_startupinfo.dwFlags = subprocess.CREATE_NEW_CONSOLE | subprocess.STARTF_USESHOWWINDOW
            self.popen_startupinfo.wShowWindow = subprocess.SW_HIDE
        else:
            self.popen_startupinfo = None

        # clear status
        view.erase_status(KEY)
        self.errors = []

    def __del__(self):
        if self.custom_style_fname:
            os.unlink(self.custom_style_fname)

    def format(self, selection, edit):
        """
        primary action when the plugin is triggered
        """
        self.debug('Formatting selection %r', selection)

        # retrieve selected text & dedent
        text = self.view.substr(selection)
        text, indent, trailing_nl = dedent_text(text)
        self.debug('Detected indent %r', indent)

        # encode text
        try:
            encoded_text = text.encode(self.encoding)
        except UnicodeEncodeError as err:
            msg = "You may need to re-open this file with a different encoding. Current encoding is %r." % self.encoding
            self.error("UnicodeEncodeError: %s\n\n%s", err, msg)
            return

        # run yapf
        self.debug('Running %s in %s', self.popen_args, self.popen_cwd)
        popen = subprocess.Popen(self.popen_args,
                                 stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE,
                                 stdin=subprocess.PIPE,
                                 cwd=self.popen_cwd,
                                 env=self.popen_env,
                                 startupinfo=self.popen_startupinfo)
        encoded_output, encoded_err = popen.communicate(encoded_text)
        self.debug('Exit code %d', popen.returncode)

        # handle errors (since yapf>=0.3, exit code 2 means changed, not error)
        if popen.returncode not in (0, 2):
            err = encoded_err.decode(self.encoding).replace(os.linesep, '\n')
            self.debug('Error:\n%s', err)

            # report error
            err_lines = err.splitlines()
            msg = err_lines[-1]
            if 'InternalError' in msg:
                self.error('%s', msg)
            else:
                loc = err_lines[-4]
                loc = loc[loc.find('line'):].capitalize()
                self.error('%s (%s)', msg, loc)
            return

        # decode text, reindent, and apply
        text = encoded_output.decode(self.encoding).replace(os.linesep, '\n')
        text = indent_text(text, indent, trailing_nl)
        self.view.replace(edit, selection, text)

    def debug(self, msg, *args):
        if self.settings.get('debug'):
            print('PyYapf:', msg % args)

    def error(self, msg, *args):
        msg = msg % args

        # add to status bar
        self.errors.append(msg)
        self.view.set_status(KEY, 'PyYapf: %s' % ', '.join(self.errors))
        if self.settings.get('popup_errors'):
            sublime.error_message(msg)


# pylint: disable=W0232
class YapfSelectionCommand(sublime_plugin.TextCommand):
    """
    The "yapf_selection" command formats the current selection (or the entire
    document if the "use_entire_file_if_no_selection" option is enabled).
    """

    def is_enabled(self):
        return is_python(self.view)

    def run(self, edit):
        yapf = Yapf(self.view)

        # empty selection?
        if all(s.empty() for s in self.view.sel()):
            if yapf.settings.get("use_entire_file_if_no_selection"):
                self.view.run_command('yapf_document')
            else:
                sublime.error_message('A selection is required')
            return

        # otherwise format all (non-empty) ones
        for s in self.view.sel():
            if not s.empty():
                yapf.format(s, edit)


# pylint: disable=W0232
class YapfDocumentCommand(sublime_plugin.TextCommand):
    """
    The "yapf_document" command formats the current document.
    """

    def is_enabled(self):
        return is_python(self.view)

    def run(self, edit):
        s = sublime.Region(0, self.view.size())
        Yapf(self.view).format(s, edit)


class EventListener(sublime_plugin.EventListener):
    def on_pre_save(self, view):
        settings = sublime.load_settings("PyYapf.sublime-settings")
        if settings.get('on_save'):
            view.run_command('yapf_document')
