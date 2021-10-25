from SPARQLWrapper import SPARQLWrapper, JSON, POST
from owlready2.base import *
from owlready2.driver import BaseMainGraph, BaseSubGraph
from owlready2.driver import _guess_format, _save
from owlready2.util import FTS, _LazyListMixin
from owlready2.base import _universal_abbrev_2_iri, _universal_iri_2_abbrev, _next_abb

from .utils import QueryGenerator
from .subgraph import SparqlSubGraph
from time import time
import multiprocessing
import re


class SparqlGraph(BaseMainGraph):
    _SUPPORT_CLONING = True

    def __init__(self, endpoint: str, world=None, default_graph=None):
        self.endpoint = endpoint
        self.world = world
        self.default_graph = default_graph

        self.client = SPARQLWrapper(endpoint, returnFormat=JSON)
        self.client.setMethod(POST)
        self.update_client = SPARQLWrapper(endpoint + '/statements')
        self.update_client.setMethod(POST)

        self.storid2iri = {}
        self.iri2storid = {}
        self.c2ontology = {
            # 0 is reserved for blank nodes in owlready2
        }
        self.graph_iri2c = {}
        if default_graph:
            self.c2ontology[1] = self.world.get_ontology(default_graph)
            self.graph_iri2c[default_graph] = 1

        self.curr_blank = 0
        self.named_graph_iris = []

        self.lock = multiprocessing.RLock()
        self.lock_level = 0

    def execute(self, *query):
        import inspect
        print(f'Called from: {type(self).__name__}.{inspect.currentframe().f_back.f_code.co_name}(' + ', '.join(
            inspect.currentframe().f_back.f_code.co_varnames) + ')')
        prev_time = time()
        print(f"execute\n{';'.join(query)}")

        # Check which client to use
        client = self.client if re.search('select[\w\W]+where[\w\W]+{[\w\W]+}',
                                          ';'.join(query).lower()) else self.update_client
        client.setMethod(POST)
        client.setQuery(';'.join(query))
        try:
            result = client.query().convert()
            print(f"took {round((time() - prev_time) * 1000)}ms")

            # Post processing
            if client == self.client:
                for item in result["results"]["bindings"]:
                    for entity_name in ['s', 'p', 'o']:
                        entity = item.get(entity_name)
                        if not entity:
                            continue
                        # Pre-Abbreviate uri
                        if entity["type"] == 'uri':
                            entity["storid"] = self._abbreviate(entity["value"])
                        # process blank node id
                        elif entity["type"] == 'bnode':
                            entity["storid"] = -int(item[entity_name + 'id']["value"])
                            # print(f'Got blank node {entity["storid"]}')
                        # assign datatype for literal and storid 'd' for datatype
                        elif entity["type"] == 'literal':
                            if not entity.get('datatype'):
                                entity['datatype'] = "http://www.w3.org/2001/XMLSchema#string"  # default is string
                            entity['d'] = self._abbreviate(entity['datatype'])

            return result
        except:
            print('error with the above sparql query')
            raise

    def acquire_write_lock(self):
        self.lock.acquire()
        self.lock_level += 1

    def release_write_lock(self):
        self.lock_level -= 1
        self.lock.release()

    def has_write_lock(self):
        return self.lock_level

    def parse(self, f):
        raise NotImplementedError

    def save(self, f, format="rdfxml", **kargs):
        raise NotImplementedError

    def set_indexed(self, indexed):
        raise NotImplementedError

    def close(self):
        raise NotImplementedError

    def fix_base_iri(self, base_iri, c=None):
        if base_iri.endswith("#") or base_iri.endswith("/"):
            return base_iri
        else:
            raise ValueError("'base_iri' must end with '/' or '#'")

    def sub_graph(self, onto):
        print("create new sub_graph with graph IRI " + onto.graph_iri)
        c = max([0, *[int(i) for i in self.c2ontology.keys()]]) + 1
        self.c2ontology[c] = onto
        self.graph_iri2c[onto.graph_iri] = c
        is_new = onto.graph_iri not in self.named_graph_iris
        if is_new:
            self.named_graph_iris.append(onto.graph_iri)
        return SparqlSubGraph(self, onto, c), is_new
        # raise NotImplementedError

    def ontologies_iris(self):
        """
        Return all ontology/Named Graph IRIs.
        """
        result = self.execute("""
        SELECT DISTINCT ?g
        WHERE {
            GRAPH ?g { ?s ?p ?s }
        }
        """)
        iris = []
        for item in result["results"]["bindings"]:
            iris.append(item["g"]["value"])

        # Update self.named_graph_iris
        for iri in iris:
            if iri not in self.named_graph_iris:
                self.named_graph_iris.append(iri)
        return iris

    def _new_numbered_iri(self, prefix):
        """
        TODO: find a way to generate numbered IRI
        """
        raise NotImplementedError

    def _refactor(self, storid, new_iri):
        self.storid2iri[storid] = new_iri
        self.iri2storid[new_iri] = storid

    def commit(self):
        pass

    def context_2_user_context(self, c):
        """Fake the user context(ontology)"""
        return self.c2ontology[c]

    def _abbreviate(self, iri, create_if_missing=True):
        storid = _universal_iri_2_abbrev.get(iri) or self.iri2storid.get(iri)

        # Check graph, if exists in graph, create one storid regardless of 'create_if_missing'
        if storid is None and not create_if_missing:
            result = self.execute(f"""
                select distinct ?uri
                from <http://ontology.eil.utoronto.ca/cids/cids>
                where {{
                    bind(<{iri}> as ?uri)
                    {{?uri ?p ?o.}}
                    union
                    {{?s ?uri ?o}}
                    union
                    {{?s ?p ?uri}}
                }}
            """)
            if len(result["results"]["bindings"]) > 0:
                create_if_missing = True

        if create_if_missing and storid is None:
            storid = max([0, _next_abb, *[int(i) for i in self.storid2iri.keys()]]) + 1
            self.iri2storid[iri] = storid
            self.storid2iri[storid] = iri

        # print(storid, ' -> ', iri)
        return storid

    def _unabbreviate(self, storid):
        if storid < 0:
            print(f'!!blank node {storid}')
            return storid
        iri = _universal_abbrev_2_iri.get(storid) or self.storid2iri.get(storid)
        return iri

    def _abbreviate_all(self, *iris):
        return [self._abbreviate(iri) for iri in iris]

    def _unabbreviate_all(self, *storids):
        return [self._unabbreviate(storid) for storid in storids]

    def new_blank_node(self):
        raise NotImplementedError
        self.curr_blank -= 1
        print(f'create a new blank node with id {self.curr_blank}')
        return self.curr_blank

    def _get_obj_triples_spo_spo(self, s, p, o):
        s_iri, p_iri, o_iri = self._unabbreviate_all(s, p, o)
        query = QueryGenerator.generate_select_query(s_iri, p_iri, o_iri, is_obj=True, graph_iris=self.named_graph_iris)
        result = self.execute(query)

        for item in result["results"]["bindings"]:
            yield item["s"]["storid"], item["p"]["storid"], item["o"]["storid"]

    def _get_data_triples_spod_spod(self, s, p, o, d):
        s_iri, p_iri, d_iri = self._unabbreviate_all(s, p, d)

        query = QueryGenerator.generate_select_query(s_iri, p_iri, o, d_iri,
                                                     is_data=True, graph_iris=self.named_graph_iris)
        result = self.execute(query)

        for item in result["results"]["bindings"]:
            yield item["s"]["storid"], item["p"]["storid"], item["o"]["value"], d or item["o"].get("d")

    def _get_triples_spod_spod(self, s, p, o, d=None):
        # should not raise NotImplementedError o
        if o:
            raise TypeError("'o' should always be None")
        s_iri, p_iri, d_iri = self._unabbreviate_all(s, p, d)

        query = QueryGenerator.generate_select_query(s_iri, p_iri, None, d_iri,
                                                     is_data=True, is_obj=True, graph_iris=self.named_graph_iris)
        result = self.execute(query)

        for item in result["results"]["bindings"]:
            yield item["s"]["storid"], item["p"]["storid"], \
                  item["o"]["storid"] if item["o"]["type"] == 'uri' else item["o"]["value"], \
                  d or item["o"].get("d")

    def _get_obj_triples_cspo_cspo(self, c, s, p, o):
        raise NotImplementedError

    def _get_obj_triples_sp_co(self, s, p):
        s_iri, p_iri = self._unabbreviate_all(s, p)

        query = QueryGenerator.generate_select_query(s_iri, p_iri, is_obj=True, graph_iris=self.named_graph_iris)
        result = self.execute(query)

        for item in result["results"]["bindings"]:
            yield self.graph_iri2c[item["g"]["value"]], item["o"]["storid"]

    def _get_triples_s_p(self, s):
        raise NotImplementedError

    def _get_obj_triples_o_p(self, o):
        raise NotImplementedError

    def _get_obj_triples_s_po(self, s):
        raise NotImplementedError

    def _get_obj_triples_sp_o(self, s, p):
        s_iri, p_iri = self._unabbreviate_all(s, p)

        query = QueryGenerator.generate_select_query(s_iri, p_iri, is_obj=True, graph_iris=self.named_graph_iris)
        result = self.execute(query)

        for item in result["results"]["bindings"]:
            yield item["o"]["storid"]

    def _get_data_triples_sp_od(self, s, p):
        s_iri, p_iri = self._unabbreviate_all(s, p)

        query = QueryGenerator.generate_select_query(s_iri, p_iri, is_data=True, graph_iris=self.named_graph_iris)
        result = self.execute(query)

        for item in result["results"]["bindings"]:
            yield item["o"]["value"], item["o"].get("d")

    def _get_triples_sp_od(self, s, p):
        raise NotImplementedError

    def _get_data_triples_s_pod(self, s):
        raise NotImplementedError

    def _get_triples_s_pod(self, s):
        raise NotImplementedError

    def _get_obj_triples_po_s(self, p, o):
        p_iri, o_iri = self._unabbreviate_all(p, o)

        query = QueryGenerator.generate_select_query(None, p_iri, o_iri, is_obj=True, graph_iris=self.named_graph_iris)
        result = self.execute(query)

        for item in result["results"]["bindings"]:
            yield item["s"]["storid"]

    def _get_obj_triples_spi_o(self, s, p, i):
        raise NotImplementedError

    def _get_obj_triples_pio_s(self, p, i, o):
        raise NotImplementedError

    def _get_obj_triple_sp_o(self, s, p):
        raise NotImplementedError

    def _get_triple_sp_od(self, s, p):
        raise NotImplementedError

    def _get_data_triple_sp_od(self, s, p):
        s_iri, p_iri = self._unabbreviate_all(s, p)

        query = QueryGenerator.generate_select_query(s_iri, p_iri, limit=1, is_data=True, graph_iris=self.named_graph_iris)
        result = self.execute(query)
        item = result["results"]["bindings"][0]
        return item["o"]["value"], item["o"].get("d")

    def _get_obj_triple_po_s(self, p, o):
        raise NotImplementedError

    def _has_obj_triple_spo(self, s=None, p=None, o=None):
        s_iri, p_iri, o_iri = self._unabbreviate_all(s, p, o)

        query = QueryGenerator.generate_select_query(s_iri, p_iri, o_iri, is_obj=True, limit=1,
                                                     graph_iris=self.named_graph_iris)
        result = self.execute(query)

        return len(result["results"]["bindings"]) > 0

    def _has_data_triple_spod(self, s=None, p=None, o=None, d=None):
        s_iri, p_iri, d_iri = self._unabbreviate_all(s, p, d)

        query = QueryGenerator.generate_select_query(s_iri, p_iri, o, d_iri, is_data=True, limit=1,
                                                     graph_iris=self.named_graph_iris)
        result = self.execute(query)

        return len(result["results"]["bindings"]) > 0

    def _del_obj_triple_raw_spo(self, s, p, o):
        raise NotImplementedError

    def _del_data_triple_raw_spod(self, s, p, o, d):
        raise NotImplementedError

    def __bool__(self):
        # Reimplemented to avoid calling __len__ in this case
        return True

    def __len__(self):
        raise NotImplementedError

    def _get_obj_triples_transitive_sp(self, s, p):
        raise NotImplementedError

    def _get_obj_triples_transitive_po(self, p, o):
        raise NotImplementedError

    def restore_iri(self, storid, iri):
        self.storid2iri[storid] = iri
        self.iri2storid[iri] = storid

    def destroy_entity(self, storid, destroyer, relation_updater, undoer_objs=None, undoer_datas=None):
        raise NotImplementedError

    def _iter_ontology_iri(self, c=None):
        raise NotImplementedError

    def _iter_triples(self, quads=False, sort_by_s=False, c=None):
        raise NotImplementedError

    def get_fts_prop_storid(self):
        raise NotImplementedError

    def enable_full_text_search(self, prop_storid):
        raise NotImplementedError

    def disable_full_text_search(self, prop_storid):
        raise NotImplementedError