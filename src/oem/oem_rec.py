# -*- coding: utf-8 -*-

from __future__ import print_function

import xmlrpclib
import re
import os
import os.path
import sys
import time
import copy
import collections

from kids.cmd import cmd, msg
from kids.data.lib import half_split_on_predicate
from kids.data.graph import reorder, cycle_exists
from kids.cache import cache, hippie_hashing
from kids.xml import xml2string, xmlize, load
from kids.txt import udiff, shorten
from kids.data import mdict
from kids.ansi import aformat

from .ooop_utils import build_filters, ooop_normalize_model_name, obj2dct, xmlid2tuple, tuple2xmlid

from .tmpl import T
from . import metadata

from . import common
from . import metadata
from . import tmpl
from .field_spec import parse_field_specs, is_field_selected

STATUS_DELETED = object()
STATUS_ADDED = object()
STATUS_MODIFIED = object()


DEFAULT_SPEC = {
    '*': '*,-create_uid,-write_uid,-create_date,-write_date,-__last_update',
    'ir.action.act_window': 'name,type,res_model,view_id,view_type,view_mode,target,usage,domain,context',
   }



def remove_tag(name, tag):
    prefix = "{%s}" % tag
    if name.startswith(prefix):
        return name[len(prefix):].strip()
    return name


class Command(common.OemCommand):
    """Record list, import to module, and other record management

    You can list, import records with this command.

    """

    @cache
    @property
    def xml_id_mgr(self):
        from .xml_id_mgr import XmlIdManager
        return XmlIdManager(self.o, self.tracked_xml_ids.keys())

    @cache
    @property
    def tracked_xml_ids(self):

        error_status = {'no_error': True}

        def err_msg(mesg):
            if error_status["no_error"]:
                print("")
                error_status["no_error"] = False
            print(aformat("  W ", fg="yellow") + mesg)

        self._tracked_files = {}
        start = time.time()
        print(aformat("Loading current module's XMLs data... ",
                      attrs=["bold", ]), end="")
        sys.stdout.flush()
        res = {}
        xml_files = self.meta.get('data', [])

        for xml_file in xml_files:
            if not os.path.exists(self.file_path(xml_file)):
                err_msg("file %r referenced in data section of "
                        "``__openerp__.py`` does not exists !"
                        % xml_file)
                continue
            if xml_file.endswith(".csv"):
                err_msg("%s: skipping CSV file." % xml_file)
                continue
            xml = load(self.file_path(xml_file))
            self._tracked_files[xml_file] = {
                'xml_file_content': xml,
            }
            ## XXXvlab: will not catch complex situation
            file_deps = set()
            for elt in xml.getchildren():
                if elt.tag != "data":
                    continue
                for record in elt.getchildren():
                    if record.tag == "comment":
                        continue
                    if 'id' not in record.attrib:
                        err_msg("!! Error while reading %s: No id found !\n%s"
                                % (record.tag,
                                   xml2string(record, xml_declaration=False)))
                        continue
                    attrib_id = record.attrib['id']
                    deps = set()
                    if record.tag == "menuitem":
                        deps |= set(
                            xmlid2tuple(record.attrib[a],
                                        self.module_name)
                            for a in ['action', 'parent']
                            if record.attrib.get(a, False))
                    ## Get deps

                    deps |= set(
                        [xmlid2tuple(xmlid, self.module_name)
                         for xmlid in record.xpath(".//@ref")])
                    evals = record.xpath(".//@eval")
                    if evals:
                        ## must get ref() usages !
                        xmlids = []
                        for e in evals:
                            try:
                                xmlids.extend(common.get_refs_in_eval(e))
                            except Exception, exc:
                                err_msg(
                                    "%s: %s %s: Exception while evaluating: %r, %s"
                                    % (xml_file, record.tag, attrib_id, e, exc.msg))
                                continue
                        deps |= set(xmlid2tuple(xmlid, self.module_name)
                                    for xmlid in xmlids)

                    ## Check deps

                    for module, xmlid in deps:
                        if module != self.module_name:
                            ## Check that we depens of this module
                            if module not in self.meta['depends']:
                                self.meta['depends'].append(module)
                                err_msg("%s: %s %s dependency to module %s not satisfied or explicited." \
                                        % (xml_file, record.tag, attrib_id, module))
                        else:
                            t = self.xmlid2tuple(xmlid)
                            if t not in res and not t[1].startswith("model_"):
                                err_msg("%s: %s %s references %s.%s which is not defined (yet?)." \
                                        % (xml_file, record.tag, attrib_id, module, xmlid))

                    ## Check for duplicate xmlid:
                    local_xml_id = self.xmlid2tuple(attrib_id)
                    if local_xml_id in res:
                        err_msg("%s: %s %s already defined in file %s." \
                                % (xml_file, record.tag, attrib_id, res[local_xml_id]['filename']))

                    res[self.xmlid2tuple(attrib_id)] = {
                        'filename': xml_file,
                        'record_xml': record,
                        'deps': deps,
                    }

                    file_deps |= deps
                    ## Check cyclicity

                    if cycle_exists(self.xmlid2tuple(attrib_id),
                                    lambda n: list(res.get(n, {'deps': []})['deps'])):
                        err_msg("%s: %s %s introduce a cyclic reference."
                                % (xml_file, record.tag, attrib_id))

            self._tracked_files[xml_file]["deps"] = file_deps

        if error_status["no_error"] is False:
            print("    ...", end="")
        print(aformat("done", attrs=["bold", ]) + " in %.3fs. (%d files, %d records)"
              % (time.time() - start, len(xml_files), len(res)))
        self._tracked_xml_ids = res
        return res

    @property
    def tracked_files(self):
        if hasattr(self, "_tracked_files"):
            return self._tracked_files
        self.tracked_xml_ids
        return self._tracked_files

    def _record_info(self, record):
        dct = obj2dct(record)
        dct["digest"] = common.ooop_object_digest(record, 50)
        xml_id = self.xml_id_mgr.lookup(record)
        dct["xml_id"] = "" if xml_id is None else self.tuple2xmlid(xml_id)
        return dct

    @cmd
    def import_(self, db, model,
                name=None, since=None, tag=None,
                fields="", xmlid=None, id=None,
                all=None,
                label='%(_model)s_record',
                fmt='%(id)5s %(name)-40s %(xml_id)-40s',
                exclude_o2m=None):
        """Import records of a given model

        Usage:
          %(std_usage)s
          %(surcmd)s DB MODEL [--name NAME]
              [--since SINCE] [--tag TAG]
              [--id NID] [--xmlid XMLID]
              [--fields FIELDS]
              [--all | -a]
              [--exclude-o2m | -x]
              [--label TMPL]
              [--fmt TMPL]

        Options:
            DB               Database identifier, either an alias or a full
                             specifier: DBNAME[@HOST[:PORT]]
            --name NAME      Filter records by name (uses ``ilike`` operator)
            --since DATE     Filter records by date
            --tag TAG        Filter records by tag (XXX doc needed on that)
            --id ID          Pick record by its id
            --xmlid XMLID    Pick record by its xmlid
            --fields FIELDS  Select fields to import (use comma to separate
                             field label). Default uses predefined values.
            --all, -a        Import all matching records. Without this,
                             %(surcmd)s will complain when more than one
                             record matches given filters.
            --exclude-o2m, -x
                             Do not recurse in o2m links of target records.
            --label TMPL     Provide template for automatic file name.
                             (Default is '%%(model)s_record')
            --fmt TMPL       Provide template for command line display of
                             records when needed. This happens when more
                             than one records is selected and ``-a`` is
                             not set.
                             (Default is '%%(id)5s %%(name)-40s %%(xml_id)-40s')

        """

        self.initialize(db=db, load_models=True,
                        interactive="__env__" in args)

        if not self.o.model_exists(model):
            msg.die("Model %r not found." % (model,))

        if xmlid:
            xmlid_tuple = self.xmlid2tuple(xmlid)
            ooop_record = self.o.get_object_by_xmlid(xmlid_tuple)
            if id:
                msg.die("Can't use ``--xmlid`` option with ``--id`` argument !")
            if ooop_record is None:
                msg.die("No object found with xmlid %r in database %s. "
                        % (self.tuple2xmlid(xmlid_tuple), db))
            if model != ooop_record._model:
                msg.die(
                    "Object found with xmlid %r has model %s not model %s. "
                    % (self.tuple2xmlid(xmlid_tuple),
                       ooop_record._model, model))
            model = ooop_record._model
            id = ooop_record._ref

        kwargs = {
            "name": name,
            "since": since,
            "id": id, "tag": tag,
        }
        l = self.o.simple_filters(model, **kwargs)

        if len(l) == 0:
            msg.die("Filter %r yielded no matching "
                    "candidate view." % build_filters(kwargs))
        if len(l) != 1:
            if not all:
                exact_matches = [r for r in l
                                 if getattr(r, 'name', False) == name]
                if len(exact_matches) != 1:
                    msg.err("View name filter %r yielded too much matching "
                            "candidate views:\n" % build_filters(kwargs))
                    for r in l:
                        print(fmt % self._record_info(r))
                    exit(1)

                l = exact_matches

        self.field_cli_specs = parse_field_specs(fields, model)

        self._record_import(l, label, tag, follow_o2m=not exclude_o2m)

    def _get_file_name_for_record(self, ooop_record, import_data,
                                  label="%(_model)s_record"):
        model = ooop_record._model
        dct = obj2dct(ooop_record)
        dct["_model"] = model[2:] if model.startswith('x_') else model
        f = label % dct
        return "%s.xml" % common.file_normalize_model_name(f)

    def _record_import(self, ooop_records, label, tag, follow_o2m=True):

        self.tracked_xml_ids  ## force creation of cache

        print(aformat("Collecting records in %s" % self.db_identifier, attrs=["bold", ]))
        content = self.to_xml(ooop_records, follow_o2m=follow_o2m, tag=tag)

        xmls = [(r, xmlize(c), d) for r, c, d in content]

        def msg(action, xmlid, filename, record):
            token = aformat("..", fg="black", attrs=["bold", ])
            trunc = lambda s, l, index=-1: shorten(s, l, index=index,
                                                   token=token, token_length=2)
            color = {"nop": {"fg": "blue"},
                     "new": {"fg": "green"},
                     "chg": {"fg": "yellow"},
                     }
            action_colored = aformat(action, **color[action])
            print("  %-4s: %-32s in %-32s (%s,%4d)%s"
                  % (action_colored,
                     trunc(self.tuple2xmlid(xmlid), 32, index=8),
                     trunc(filename, 32, index=8),
                     record._model, record._ref,
                     (": %s" % r.name) if 'name' in r.fields else ''))

        print(aformat("Reviewing collected records", attrs=["bold", ]))
        records_written = []
        filenames = {}
        for record, xml, deps in xmls:
            records_written.append(record)
            module, xml_id = self.xml_id_mgr.lookup(record)
            ## This is the real xmlid that will be written and should
            ## be checked
            xmlid = self.xmlid2tuple(xml_id)
            if xmlid in self.tracked_xml_ids:
                elt = self.tracked_xml_ids[xmlid]['record_xml']

                filename = self.tracked_xml_ids[xmlid]['filename']
                if xml2string(elt) == xml2string(xml):
                    msg("nop", xmlid, filename, record)
                    continue
                msg("chg", xmlid, filename, record)
                if filename not in filenames:
                    filenames[filename] = \
                        self.tracked_files[filename]['xml_file_content']
                ## find 'data' element (parent) of tracked xml
                elt = self.tracked_xml_ids[xmlid]['record_xml']
                data = elt.getparent()
                data.replace(elt, xml)
                self.tracked_xml_ids[xmlid]['record_xml'] = xml
                self.tracked_xml_ids[xmlid]['replaced'] = \
                    self.tracked_xml_ids[xmlid].get('replaced', 0) + 1
            else:
                filename = self._get_file_name_for_record(record, xmls, label)
                msg("new", xmlid, filename, record)
                if filename not in filenames:
                    filenames[filename] = \
                        self.tracked_files[filename]['xml_file_content'] \
                        if filename in self.tracked_files else \
                        common._empty_data_xml()
                ## find 'data' xml element.
                data = filenames[filename].getchildren()[0]
                data.append(xml)

        if filenames:
            print(aformat("Writing changes", attrs=["bold", ]))
        else:
            print(aformat("No changes to write to files.", attrs=["bold", ]))

        for filename, data in filenames.items():
            self.add_xml(filename, xml2string(data))

        for r in records_written:
            self._trigger_event(r, 'write')

    def menu_to_xml(self, menu, xml_id, follow_o2m=False, tags=False):
        content = []
        action = None
        action_xml_id = None
        deps = []

        if menu.action:
            action = self.o.get_object(*menu.action.split(","))
            omodel = action.res_model
            lookup_action = self.xml_id_mgr.lookup(action._model, action._ref)
            if lookup_action is None:
                ## we'll then try to import action also
                content.extend(self.to_xml(
                    [action],
                    follow_o2m=True))
                lookup_action = self.xml_id_mgr.lookup(action._model,
                                                       action._ref)
                assert lookup_action is not None
            deps.append(lookup_action)
            action_xml_id = self.tuple2xmlid(lookup_action)

        _module, xml_id = self.xml_id_mgr.create(
            self.module_name,
            menu._model, menu._ref, menu.name)
        if menu.parent_id:
            lookup_menu = self.xml_id_mgr.lookup(menu.parent_id._model,
                                                 menu.parent_id._ref)
            if lookup_menu is None:
                print("  !! no xml id for parent menu %r (%s,%s) of menu %r"
                      % (menu.parent_id.name, menu.parent_id._model,
                         menu.parent_id._ref, menu.name))
                parent_xml_id = False
            else:
                deps.append(lookup_menu)
                parent_xml_id = self.tuple2xmlid(lookup_menu)
        else:
            parent_xml_id = False
        groups = False
        if menu.groups_id:
            groups = []
            for g in menu.groups_id:
                lookup_xml_id = self.xml_id_mgr.lookup(g._model, g._ref)
                if lookup_xml_id is None:
                    ## we'll then try to import this menu also
                    content.extend(self.to_xml(
                        [g], follow_o2m=True, tag=tags))
                    lookup_xml_id = self.xml_id_mgr.lookup(g._model, g._ref)
                    assert lookup_xml_id is not None
                deps.append(lookup_xml_id)
                groups.append(lookup_xml_id)
            groups = ",".join([self.tuple2xmlid(lookup_xml_id)
                               for lookup_xml_id in groups])

        content.append(
            (menu,
             tmpl.render(
                 tmpl.OPENERP_MENU_TEMPLATE,
                 xml_id=xml_id,
                 m=menu, action=action,
                 action_xml_id=action_xml_id,
                 groups=groups,
                 parent_xml_id=parent_xml_id,
                 file_normalize_model_name=common.file_normalize_model_name),
             deps))
        return content

    @cache
    @property
    def field_specs(self):
        """Return current field specs"""

        spec = copy.deepcopy(self.field_cli_specs or {})

        cfg_spec = DEFAULT_SPEC.copy()
        cfg_spec.update(mdict.mdict(self.cfg).get("rec.import.fields", {}))
        cfg_spec = ";".join("%s:%s" % (m, fs) for m, fs in cfg_spec.items())
        spec.update(parse_field_specs(cfg_spec))
        return spec

    @cache
    def get_fields_for_model(self, model):
        """Return list of fields to import"""

        fields = [f for f in self.o.get_fields(model)
                  if is_field_selected(model, f, self.field_specs)]

        ## order

        mcfg = mdict.mdict(self.cfg)
        default_rank_cfg = mcfg.get("rec.import.order.*", 'name,sequence')
        rank = mcfg.get("rec.import.order.%s" % model, default_rank_cfg)
        ## force 'name', then 'sequence' to be first field displayed...
        order_rank = dict((label, i)
                          for i, label in enumerate(rank.split(',')))
        fields.sort(key=lambda x: order_rank.get(x[0], x[0]))
        return fields

    @cache
    def get_fields_def_for_model(self, model):
        """Return list of fields to import"""
        return collections.OrderedDict(
            (k, v) for k, v in self.o.get_fields(model).items()
            if k in self.get_fields_for_model(model))

    def to_xml(self, records, follow_o2m=False, tag=False):

        def msg(action, xmlid, record):
            token = aformat("..", fg="black", attrs=["bold", ])
            trunc = lambda s, l, index=-1: shorten(s, l, index=index,
                                                   token=token, token_length=2)
            color = {
                "grab": {"fg": "cyan"},
                "skip": {"fg": "blue"},
                }
            action_colored = aformat(action, **color[action])
            print("  %s: %-64s     (%s,%4d)%s"
                  % (action_colored,
                     trunc(self.tuple2xmlid(xmlid), 64),
                     record._model, record._ref,
                     (": %s" % r.name) if 'name' in r.fields else ''))

        content = []
        objs = [(record, record._model, getattr(record, 'name', 'anonymous'))
                for record in records]
        done = []

        while objs:

            (r, model, identifier), objs = objs[0], objs[1:]

            exported_fields = list((k, v) for k, v in r.fields.iteritems()
                                   if k in self.get_fields_for_model(model))

            ## XXXvlab: Warning, nothing is done to ensure uniqueness within
            ## the current XML. Hopefully, names will distinguish them out.
            lookup = self.xml_id_mgr.lookup(r)
            if lookup:
                module, xml_id = lookup
            else:
                module, xml_id = self.xml_id_mgr.create(
                    self.module_name,
                    model, r._ref, identifier)
                self.o.set_xml_id(model, r._ref, (module, xml_id))

            if (module, xml_id) in done:
                msg("skip", (module, xml_id), r)
                continue

            ##
            ## Remove markups (tags) and set xml_id in current database
            ##

            msg("grab", (module, xml_id), r)

            if tag and 'name' in r.fields:
                ## change only in current lang
                r.name = remove_tag(r.name, tag)

                def _save(r):
                    lang = self.o.context.get('lang', 'en_US')
                    if lang != 'en_US':
                        old_lang = self.o.context['lang']
                        del self.o.context['lang']
                        ## save only the name attribute to avoid overwriting
                        ## other values
                        current_name = self.o.read(r._model, r._ref,
                                                   ['name']).get('name', None)
                        if current_name is not None:
                            new_name = remove_tag(current_name, tag)
                            self.o.write(r._model, [r._ref],
                                         {'name': new_name})
                            print("  renamed object %r in %r (new name: %r)"
                                  % (r, 'en_US', new_name))
                        self.o.context['lang'] = old_lang
                    try:
                        ## save only the name attribute to avoid overwriting
                        ## other values
                        self.o.write(r._model, [r._ref], {'name': r.name})
                        print("  renamed object %r in %r (new name: %r)"
                              % (r, lang, r.name))
                    except xmlrpclib.Fault, e:
                        if re.search("^warning -- Constraint Error.*"
                                     "Language code.*known languages",
                                     e.faultCode, re.DOTALL):
                            pass
                            #print("  warning: current database does not support language %r" % self.o.context['lang'])
                        else:
                            raise

                self._add_callback(r, 'write', _save)

            ##
            ## Generate XML for a record
            ##

            content.extend(
                self.record_to_xml(r, xml_id, follow_o2m=follow_o2m))
            done.append((module, xml_id))
            if follow_o2m:
                ## Add all the one2many:
                for f, fdef in exported_fields:
                    if fdef['ttype'] != 'one2many':
                        continue
                    new_records = getattr(r, f)
                    if new_records:
                        ## big mess to get the element that do not have any
                        ## xml_id to the end of a classical sort.
                        with_xmlids, without_xmlids = half_split_on_predicate(
                            new_records,
                            lambda obj: self.xml_id_mgr.lookup(obj) is None)
                        with_xmlids.sort(key=self.xml_id_mgr.get_xml_id_sort_key)
                        print("    + %d o2m descendant along %r attribute"
                              % (len(new_records), f))
                        objs += [(obj, fdef['relation'], identifier)
                                 for obj in (with_xmlids + without_xmlids)]
        return content

    def record_to_xml(self, record, xml_id, follow_o2m=None):
        content = []
        model = record._model

        if model == "ir.ui.menu":
            content.extend(self.menu_to_xml(
                record, xml_id,
                follow_o2m=follow_o2m))
        else:
            content.extend(self._render_record_template(record, model, xml_id))

        return content

    def _render_record_template(self, r, model, xml_id):
        deps = []
        ## XXXvlab: couldn't we remove ``model`` in favor of r._model ?
        return [(r, tmpl.render(T / "xml" / "record.xml",
                                r=r, fields=self.get_fields_def_for_model(model).items(),
                                model=model,
                                xml_id=xml_id,
                                xml_id_mgr=self.xml_id_mgr,
                                db_identifier=self.db_identifier,
                                deps=deps), deps)]

    ## XXXvlab: should be a method of an ooop.Data adapter
    def _add_callback(self, r, event, callback):
        if not hasattr(self, 'cbs'):
            self.cbs = {}

        key = (r._model, r._ref)

        if key not in self.cbs:
            self.cbs[key] = {}

        record_events = self.cbs[key]

        if event not in record_events:
            record_events[event] = []

        record_events[event].append((r, callback))

    ## XXXvlab: should be a method of an ooop.Data adapter
    def _trigger_event(self, r, event):
        key = (r._model, r._ref)
        events = getattr(self, "cbs", {}).get(key, {}).get(event, [])
        for r, ev in events:
            ev(r)

    @cmd
    def defs(self, dbs, model):
        """Prints and diffs model schema on given databases

        Usage:
          %(std_usage)s
          %(surcmd)s DBS MODEL

        Options:
          %(std_options)s
          DBS              Database identifier(s), you can provide only one
                             or two to ask for a diff by using this syntax:
                                DBNAME1[@HOST1[:PORT1]]..DBNAME2[@HOST2[:PORT2]]
          MODEL            Odoo/OpenERP Model name

        """
        dbs = dbs.split("..") if ".." in dbs else [dbs]
        ooops = [self.db[db].ooop(load_models=True, interactive=True)
                 for db in dbs]

        ooop_model_name = ooop_normalize_model_name(model)
        mgrs = [getattr(ooop._ooop, ooop_model_name, False) for ooop in ooops]
        if any(mgr is False for mgr in mgrs):
            for db, mgr in zip(dbs, mgrs):
                if mgr is False:
                    msg.err('model %r is not found in %r.' % (model, db))
            exit(1)

        all_field_defs = [mgr.fields_get() for mgr in mgrs]
        ## Get the common max len
        max_len_name = 1 + max([0] + [len(name)
                                      for field_defs in all_field_defs
                                      for name in field_defs])

        all_columns = [ooop.get_all_d("ir.model.fields",
                                      [('model', '=', model)])
                       for ooop in ooops]
        for columns, field_defs in zip(all_columns, all_field_defs):
            for k, field_def in field_defs.iteritems():
                field_def["ttype"] = ("function(%s)" % field_def["type"]) \
                                     if field_def.get('function', False) else \
                                     field_def["type"]
                # find column
                for col in columns:
                    if col["name"] == k:
                        break
                if col["name"] != k:
                    field_def["nodef"] = True
                    continue

                field_def["required"] = col["required"]
                field_def["domain"] = col["domain"]
                field_def["readonly"] = col["readonly"]
                field_def["size"] = col["size"]
                field_def["translate"] = col["translate"]

        if len(dbs) == 2:
            key = lambda x: x[0]
        else:
            key = lambda k: (k[1]["ttype"], 0) if k[0] == "name" \
                  else (k[1]["ttype"], k[0])

        outputs = [[] for _ in dbs]
        for output, field_defs in zip(outputs, all_field_defs):
            for name, field_def in sorted(field_defs.items(), key=key):
                line = ""
                field_def["name"] = ("%%-%ds" % max_len_name) % name
                line += ("%(name)s" % field_def)
                if "nodef" in field_def:
                    line += "???????????? "
                else:
                    line += (" ".join([
                        "REQ" if field_def["required"] else "   ",
                        "RO" if field_def["readonly"] else "  ",
                        "T" if field_def["translate"] else " ",
                        ("%04d" % field_def["size"]) if field_def["size"] else "    "
                        ]))
                if len(dbs) == 1 and field_def.get('function', False):
                    ## could be a related also
                    line += ("function(%(type)s)" % field_def)
                else:
                    line += ("%(type)-10s" % field_def)
                if '2' in field_def['type']:
                    line += (" => %(relation)s" % field_def)
                    if "relation_fields" in field_def:
                        line += ("(%(relation_field)s)" % field_def)
                output.append(line)

        if len(dbs) == 2:
            print(udiff("\n".join(outputs[0]), "\n".join(outputs[1]),
                        dbs[0], dbs[1]))
        else:
            print("\n".join(outputs[0]))
