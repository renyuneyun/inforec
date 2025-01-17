# -*- coding:utf-8 -*-
#
#   Author  :   renyuneyun
#   E-mail  :   renyuneyun@gmail.com
#   Date    :   21/04/17 10:46:34
#   License :   Apache 2.0 (See LICENSE)
#

'''
This module contains all that is related to storage and backend data structures.
It may be split in the future.
'''

import itertools
import json
import networkx as nx
import pathlib
import uuid

from typing import Dict, Iterable, List, Mapping, Optional, Set, Union
from uuid import UUID

import sede

from exception import (
        IllegalStateError,
        )
from helper import delegate
from model import (
        AbsoluteDateTime,
        Date,
        Event,
        RelTimeMarker,
        RelTimeSpecImplicit,
        TimeRelativity,
        )


DATABASE_FILE = 'db.json'

K_COLLECTION = 'collection'
K_TYPE = 'type'
K_DATA = 'data'
T_EVENT = 'event'
T_ABSOLUTEDATETIME = 'absolute_date_time'
T_DATE = 'date'
M_T_DES = {
        T_ABSOLUTEDATETIME: sede.deserialize_absolutedatetime,
        T_DATE: sede.deserialize_date,
        T_EVENT: sede.deserialise_event,
        }
M_T_SER = {
        AbsoluteDateTime: (sede.serialise_event, T_ABSOLUTEDATETIME),
        Date: (sede.serialize_date, T_DATE),
        Event: (sede.serialise_event, T_EVENT),
        }


class Collection:

    def __init__(self, initial_rel_markers: Iterable[RelTimeMarker]=[]):
        self.collection = {}  # type: Dict[UUID, RelTimeMarker]
        self._dangling_refs = {}  # type: Dict[UUID, Set[UUID]]
        self.add_item(*initial_rel_markers)

    def _do_dangling_ref(self, timespec, item_id):
        for tid in itertools.chain(timespec.befores or [], timespec.sames or [], timespec.afters or []):
            if tid in self.collection:
                continue
            if tid not in self._dangling_refs: self._dangling_refs[tid] = set()
            self._dangling_refs[tid].add(item_id)

    def add_item(self, *item: RelTimeMarker) -> None:
        for s_item in item:
            iid = s_item.id
            if iid in self.collection:
                raise IllegalStateError('The item you are trying to add has duplicated id with an existing entry.')
            self.collection[iid] = s_item
        for s_item in item:
            iid = s_item.id
            if isinstance(s_item, Event):
                if iid in self._dangling_refs:
                    del self._dangling_refs[iid]
                self._do_dangling_ref(s_item.timespec, iid)

    def update_item(self, item_id: Union[UUID, str], new_item: RelTimeMarker) -> None:
        if not isinstance(item_id, UUID):
            item_id = UUID(item_id)
        old_item = self.get_item(item_id)
        assert isinstance(new_item, type(old_item))
        self.collection[old_item.id] = new_item
        for iids in self._dangling_refs.values():
            iids.discard(item_id)
        if isinstance(new_item, Event):
            self._do_dangling_ref(new_item.timespec, item_id)

    def is_self_contained(self) -> bool:
        '''
        Test if the collection is self-contained, which means every event points to a valid event in the collection.
        '''
        return not bool(self._dangling_refs)

    def get_item(self, id: Union[UUID, str]) -> RelTimeMarker:
        if not isinstance(id, UUID):
            id = UUID(id)
        return self.collection[id]

    def get_event(self, id: Union[UUID, str]) -> Event:
        item = self.get_item(id)
        if not isinstance(item, Event):
            raise RuntimeError("The requested item {} is not an Event, but a {}".format(id, type(item)))
        return item
        
    def list(self) -> Iterable[UUID]:
        return self.collection.keys()

    def has_no_conflict(self) -> bool:
        try:
            return not bool(self.conflicts())
        except nx.NetworkXUnfeasible:
            return False
        return True

    def conflicts(self):
        ordered_events = OrderedMarkers(self)
        return ordered_events.cycles()


# ForeverPast = RelTimeMarker()
# ForeverFuture = RelTimeMarker()


class OrderedMarkers:
    def __init__(self, collection: Collection):
        g = nx.DiGraph()

        # 並查集建立
        def current_root(node):
            if id_merging[node] == node: return node
            return current_root(id_merging[node])
        coll = collection.collection.values()
        id_merging = {}  # k:v <==> event ID : the event ID to a parent node of its group
        for marker in coll:
            id_merging[marker.id] = marker.id
        for marker in coll:
            if isinstance(marker, Event):
                sames = marker.timespec.sames
                if sames:
                    merged_id = None
                    for same in sames:
                        if id_merging[same] != same:
                            merged_id = id_merging[same]
                            break
                    if merged_id:
                        for same in sames:
                            root = current_root(same)
                            id_merging[same] = merged_id
                        id_merging[marker.id] = merged_id
        id_merged = {}
        for marker in coll:
            id_merged[marker.id] = current_root(marker.id)
        # 並查集建立完畢

        implicits = []
        for marker in coll:
            if isinstance(marker, RelTimeSpecImplicit):
                implicits.append(marker)
        implicit_befores = {}
        implicit_afters = {}
        for marker in implicits:
            befores = []
            afters = []
            for m2 in implicits:
                if marker is not m2:
                    rel = marker.compare(m2)
                    if rel == TimeRelativity.BEFORE:
                        befores.append(m2.id)
                        g.add_edge(marker.id, m2.id)
                    elif rel == TimeRelativity.AFTER:
                        afters.append(m2.id)
                        g.add_edge(m2.id, marker.id)
            implicit_befores[marker.id] = befores
            implicit_afters[marker.id] = afters


        for marker in coll:
            if isinstance(marker, Event):
                node_id_1 = str(id_merged[marker.id])
                afters = marker.timespec.afters
                if afters:
                    for after in afters:
                        node_id_2 = str(id_merged[after])
                        g.add_edge(node_id_2, node_id_1)
                befores = marker.timespec.befores
                if befores:
                    for before in befores:
                        node_id_2 = str(id_merged[before])
                        g.add_edge(node_id_1, node_id_2)

        self.g = g

    def cycles(self):
        return list(nx.simple_cycles(self.g))


class InfoRecDB:

    @staticmethod
    def not_exists_or_empty_dir(dir_path):
        path = pathlib.Path(dir_path)
        if not path.exists(): return True
        if not path.is_dir(): return False
        subs = list(path.glob('*'))
        return not bool(subs)

    @staticmethod
    def read_db(directory):
        path = pathlib.Path(directory) / DATABASE_FILE
        with open(path, 'r') as f:
            dic = json.load(f)
            coll = []
            for entry in dic[K_COLLECTION]:
                t = entry[K_TYPE]
                assert t in M_T_DES, "DB with unexpected schema: Unknown type {} in collection".format(t)
                marker = M_T_DES[t](entry[K_DATA])
                coll.append(marker)
            return Collection(coll)

    @classmethod
    def init(cls, base_dir):
        if not cls.not_exists_or_empty_dir(base_dir):
            raise RuntimeError(f'Path `{base_dir}` is not an empty directory or is a file')

        path = pathlib.Path(base_dir)
        if not path.exists():
            path.mkdir()
        collection = Collection()
        db = cls(base_dir, collection)
        db.write()

    @classmethod
    def open(cls, base_dir, auto_init=False):
        if auto_init:
            if cls.not_exists_or_empty_dir(base_dir):
                cls.init(base_dir)
        collection = cls.read_db(base_dir)
        return cls(base_dir, collection)

    def __init__(self, directory, collection):
        self._dir = directory
        self.collection = collection

    def write(self):
        path = pathlib.Path(self._dir) / DATABASE_FILE
        dic = {}
        coll = []
        for marker in self.collection.collection.values():
            t = type(marker)
            assert t in M_T_SER, "Collection contains unknown type {}".format(t)
            entry = {
                    K_TYPE: M_T_SER[t][1],
                    K_DATA: M_T_SER[t][0](marker),
                    }
            coll.append(entry)
        dic[K_COLLECTION] = coll
        with open(path, 'w') as f:
            json.dump(dic, f)


class App:
    def __init__(self, db_dir, auto_init=True):
        self.db = InfoRecDB.open(db_dir, auto_init)

    def collection(self):
        return self.db.collection

    def flush(self):
        self.db.write()
