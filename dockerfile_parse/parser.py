# -*- coding: utf-8 -*-
"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from __future__ import print_function, unicode_literals

import json
import logging
import os
import re
import shlex
try:
    # py3
    from shlex import quote
except ImportError:
    from pipes import quote

from .constants import DOCKERFILE_FILENAME, PY2

logger = logging.getLogger(__name__)


class DockerfileParser(object):
    def __init__(self, path=None, cache_content=False):
        """
        Initialize path to Dockerfile
        :param path: path to (directory with) Dockerfile
        :param cache_content: cache Dockerfile content inside DockerfileParser
        """
        path = path or '.'
        if path.endswith(DOCKERFILE_FILENAME):
            self.dockerfile_path = path
        else:
            self.dockerfile_path = os.path.join(path, DOCKERFILE_FILENAME)

        self.cache_content = cache_content
        self.cached_content = ''  # unicode string

        if cache_content:
            try:
                # this will cache the Dockerfile content
                self.content
            except (IOError, OSError):
                # the Dockerfile doesn't exist yet
                pass

    @staticmethod
    def b2u(string):
        """ bytes to unicode """
        if isinstance(string, bytes):
            return string.decode('utf-8')
        return string

    @staticmethod
    def u2b(string):
        """ unicode to bytes (Python 2 only) """
        if PY2 and isinstance(string, unicode):
            return string.encode('utf-8')
        return string

    @property
    def lines(self):
        """
        :return: list containing lines (unicode) from Dockerfile
        """
        if self.cache_content and self.cached_content:
            return self.cached_content.splitlines(True)

        try:
            with open(self.dockerfile_path, 'r') as dockerfile:
                lines = [self.b2u(l) for l in dockerfile.readlines()]
                if self.cache_content:
                    self.cached_content = ''.join(lines)
                return lines
        except (IOError, OSError) as ex:
            logger.error("Couldn't retrieve lines from dockerfile: %s" % repr(ex))
            raise

    @lines.setter
    def lines(self, lines):
        """
        Fill Dockerfile content with specified lines
        :param lines: list of lines to be written to Dockerfile
        """
        if self.cache_content:
            self.cached_content = ''.join([self.b2u(l) for l in lines])

        try:
            with open(self.dockerfile_path, 'w') as dockerfile:
                dockerfile.writelines([self.u2b(l) for l in lines])
        except (IOError, OSError) as ex:
            logger.error("Couldn't write lines to dockerfile: %s" % repr(ex))
            raise

    @property
    def content(self):
        """
        :return: string (unicode) with Dockerfile content
        """
        if self.cache_content and self.cached_content:
            return self.cached_content

        try:
            with open(self.dockerfile_path, 'r') as dockerfile:
                content = self.b2u(dockerfile.read())
                if self.cache_content:
                    self.cached_content = content
                return content
        except (IOError, OSError) as ex:
            logger.error("Couldn't retrieve content of dockerfile: %s" % repr(ex))
            raise

    @content.setter
    def content(self, content):
        """
        Overwrite Dockerfile with specified content
        :param content: string to be written to Dockerfile
        """
        if self.cache_content:
            self.cached_content = self.b2u(content)

        try:
            with open(self.dockerfile_path, 'w') as dockerfile:
                dockerfile.write(self.u2b(content))
        except (IOError, OSError) as ex:
            logger.error("Couldn't write content to dockerfile: %s" % repr(ex))
            raise

    @property
    def structure(self):
        """
        Returns a list of dicts describing the commands:
        [
            {"instruction": "FROM",       # always upper-case
             "startline": 0,              # 0-based
             "endline": 0,                # 0-based
             "content": "From fedora\n",
             "value": "fedora"},

            {"instruction": "CMD",
             "startline": 1,
             "endline": 2,
             "content": "CMD yum -y update && \\\n    yum clean all\n",
             "value": "yum -y update && yum clean all"}
        ]

        Comments are ignored.
        """
        def _rstrip_backslash(l):
            l = l.rstrip()
            if l.endswith('\\'):
                return l[:-1]
            return l

        instructions = []
        lineno = -1
        insnre = re.compile(r'^\s*(\w+)\s+(.*)$')  # matched group is insn
        contre = re.compile(r'^.*\\\s*$')          # line continues?
        in_continuation = False
        current_instruction = None
        for line in self.lines:
            lineno += 1
            if not in_continuation:
                m = insnre.match(line)
                if not m:
                    continue

                current_instruction = {'instruction': m.groups()[0].upper(),
                                       'startline': lineno,
                                       'endline': lineno,
                                       'content': line,
                                       'value': _rstrip_backslash(m.groups()[1])}
            else:
                current_instruction['content'] += line
                current_instruction['endline'] = lineno
                if current_instruction['value']:
                    current_instruction['value'] += _rstrip_backslash(line)
                else:
                    current_instruction['value'] = _rstrip_backslash(line.lstrip())

            in_continuation = contre.match(line)
            if not in_continuation and current_instruction is not None:
                instructions.append(current_instruction)

        return instructions

    @property
    def json(self):
        """
        :return: JSON formatted string with instructions & values from Dockerfile
        """
        insndescs = [{insndesc['instruction']: insndesc['value']} for insndesc in self.structure]
        return json.dumps(insndescs)

    @property
    def baseimage(self):
        """
        :return: base image, i.e. value of FROM instruction
        """
        for insndesc in self.structure:
            if insndesc['instruction'] == 'FROM':
                return insndesc['value']
        return None

    @baseimage.setter
    def baseimage(self, value):
        """
        setter for 'FROM' instruction

        """
        self._modify_instruction('FROM', value)

    @property
    def cmd(self):
        """
        There can only be one CMD instruction in a Dockerfile.
        If there's more than one CMD then only the last CMD takes effect.
        :return: value of last CMD instruction
        """
        value = None
        for insndesc in self.structure:
            if insndesc['instruction'] == 'CMD':
                value = insndesc['value']
        return value

    @cmd.setter
    def cmd(self, value):
        """
        setter for 'CMD' instruction

        """
        self._modify_instruction('CMD', value)

    @property
    def labels(self):
        """
        LABELs from Dockerfile
        :return: dictionary of label:value (value might be '')
        """
        labels = {}
        for insndesc in self.structure:
            if insndesc['instruction'] == 'LABEL':
                shlex_splits = self._shlex_split(insndesc['value'])
                if '=' not in shlex_splits[0]:  # LABEL name value
                    # remove (double-)quotes
                    value = insndesc['value'].replace("'", "").replace('"', '')
                    # split it to first and the rest
                    key_val = value.split(None, 1)
                    labels[key_val[0]] = key_val[1] if len(key_val) > 1 else ''
                    logger.debug("new label %s=%s", repr(key_val[0]), repr(labels[key_val[0]]))
                else:  # LABEL "name"="value"
                    for token in shlex_splits:
                        key_val = token.split("=", 1)
                        labels[key_val[0]] = key_val[1] if len(key_val) > 1 else ''
                        logger.debug("new label %s=%s", repr(key_val[0]), repr(labels[key_val[0]]))
        return labels

    @labels.setter
    def labels(self, labels):
        """
        Setter for LABEL instruction. Deletes old LABELs and sets new per input param.
        :param labels: dictionary of label name & value to be set
        """
        if not isinstance(labels, dict):
            raise TypeError('labels needs to be a dictionary {label name: label value}')

        self._delete_instructions('LABEL')
        for key, value in labels.items():
            self._add_instruction('LABEL', (key, value))

    def change_labels(self, labels):
        """
        Only changes labels that are specified in the input dict.
        You can't add or delete labels, just change value of existing ones.
        :param labels: Dictionary of label name & value you want to change.
        """
        if not isinstance(labels, dict):
            raise TypeError('labels needs to be a dictionary {label name: label value}')

        for key, value in labels.items():
            self._modify_instruction_label(key, value)

    def _shlex_split(self, string):
        """
        Python2's shlex doesn't like unicode, so we have to convert the string
        into bytes, run shlex.split() and convert it back to unicode.
        """
        if PY2 and isinstance(string, unicode):
            string = self.u2b(string)
            # this takes care of quotes
            splits = shlex.split(string)
            return map(self.b2u, splits)
        else:
            return shlex.split(string)

    def _modify_instruction_label(self, label_key, label_value):
        """
        set LABEL label_key to label_value
        """
        if label_key not in self.labels:
            raise KeyError('%s not in LABELs' % label_key)

        # Find where in the file to put the next release
        content = startline = endline = None
        for candidate in [insn for insn in self.structure
                          if insn['instruction'] == 'LABEL']:
            splits = self._shlex_split(candidate['value'])

            # LABEL syntax is one of two types:
            if '=' not in splits[0]:  # LABEL name value
                # remove (double-)quotes
                value = candidate['value'].replace("'", "").replace('"', '')
                words = value.split(None, 1)
                if words[0] == label_key:
                    # Adjust label value
                    words[1] = label_value

                    # Now reconstruct the line
                    content = " ".join(['LABEL'] + words) + '\n'
                    startline = candidate['startline']
                    endline = candidate['endline']
                    break
            else:  # LABEL "name"="value"
                for token in splits:
                    words = token.split("=", 1)
                    if words[0] == label_key:
                        # Adjust label value
                        words[1] = label_value
                        n = splits.index(token)
                        splits[n] = "=".join(map(quote, words))

                        # Now reconstruct the line
                        content = " ".join(['LABEL'] + splits) + '\n'
                        startline = candidate['startline']
                        endline = candidate['endline']
                        break

        # We know the label we're looking for is there
        assert content and startline and endline

        # Re-write the Dockerfile
        lines = self.lines
        del lines[startline:endline + 1]
        lines.insert(startline, content)
        self.lines = lines

    def _modify_instruction(self, instruction, new_value, old_value=None):
        """
        :param instruction: like 'FROM' or 'CMD'
        :param new_value: new value of instruction
        :param old_value: if not None then modify only if old value equals old_value
        """
        if instruction == 'LABEL':
            raise ValueError('Please use labels.setter')
        for insn in self.structure:
            if insn['instruction'] == instruction:
                if old_value and insn['value'] != old_value:
                    continue
                new_line = '{0} {1}\n'.format(instruction, new_value)
                lines = self.lines
                del lines[insn['startline']:insn['endline'] + 1]
                lines.insert(insn['startline'], new_line)
                self.lines = lines

    def _delete_instructions(self, instruction, value=None):
        """
        :param instruction: name of instruction to be deleted
        :param value: if specified, delete instruction only when it has this value
        """
        lines = self.lines
        deleted = False
        for insn in reversed(self.structure):
            if insn['instruction'] == instruction:
                if value and insn['value'] != value:
                    continue
                deleted = True
                del lines[insn['startline']:insn['endline'] + 1]
        if deleted:
            self.lines = lines

    def _add_instruction(self, instruction, value):
        """
        :param instruction: instruction name to be added
        :param value: instruction value
        """
        if instruction == 'LABEL' and len(value) == 2:
            new_line = '{0} "{1}"="{2}"\n'.format(instruction, value[0], value[1])
        else:
            new_line = '{0} {1}\n'.format(instruction, value)
        if new_line:
            lines = self.lines
            lines += new_line
            self.lines = lines
