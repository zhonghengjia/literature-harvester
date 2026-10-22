"""Behavioral checks for opt-out additive discovery, no network or installation."""
from contextlib import redirect_stdout, redirect_stderr
from copy import deepcopy
import importlib.util
import io
import json
from pathlib import Path
import socket
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS=Path(__file__).resolve().parents[1]/"scripts"
sys.path.insert(0,str(SCRIPTS))
from lit_harvest import pipeline
from lit_harvest.coverage_plan import build_coverage_plan
from lit_harvest.models import PaperRecord
from lit_harvest.query_plan import candidate_id,validate_plan
from lit_harvest.settings import DEFAULT_CONFIG,validate_config

def topic():
    return {"schema_version":1,"question":"Clinical hyperchloremia", "scope_note":"Original scope; no population restriction",
        "concepts":[{"id":"cl","terms":["hyperchloremia","hyperchloraemia"]},
                    {"id":"renal","terms":["kidney injury","renal injury"]}],
        "routes":[{"id":"pm-original","source":"pubmed","concept_ids":["cl"],"limit":100},
                  {"id":"epmc-original","source":"europe_pmc","concept_ids":["cl"],"limit":100}]}

def config():
    value=deepcopy(DEFAULT_CONFIG)
    value['sources']={"pubmed":True,"europe_pmc":True}
    return value

def reject_network(*_args,**_kwargs): raise AssertionError('Unexpected network in test')

class Additive090Tests(unittest.TestCase):
    def setUp(self):
        self.patches=[patch('socket.create_connection',reject_network),patch('socket.socket.connect',reject_network)]
        for item in self.patches:item.start()
        self.addCleanup(lambda:[item.stop() for item in self.patches])

    def test_original_ids_queries_and_quotas_unchanged(self):
        raw=topic();before=deepcopy(raw)
        result=build_coverage_plan(raw)
        self.assertEqual(raw,before)
        original=validate_plan(raw)['routes']
        self.assertEqual(result['query_plan']['routes'][:2],original)
        self.assertEqual(result['original_plan'],before)
        for budget in result['budgets'].values():
            self.assertEqual(budget,{'original_records':100,'additional_records':50,'allocated_records':150})
        self.assertIn('original 100 + additional 50 = 150',result['query_plan']['scope_note'])

    def test_normalization_retains_concept_subset_and_is_idempotent(self):
        normalized=validate_plan(topic())
        self.assertEqual(normalized['routes'][0]['concept_ids'],['cl'])
        self.assertEqual(validate_plan(normalized),normalized)
        result=build_coverage_plan(normalized)
        broad=[r for r in result['query_plan']['routes'] if r['id'].startswith('add_')]
        self.assertFalse(any('kidney' in r['query'] for r in broad))
        repeated=build_coverage_plan(validate_plan(result['query_plan']))
        self.assertEqual(repeated['query_plan'],result['query_plan'])
        self.assertTrue(repeated['already_expanded'])

    def test_exact_normalized_query_can_derive_but_edited_query_fails(self):
        plan=validate_plan(topic())
        for row in plan['routes']:row.pop('concept_ids')
        # These explicit routes refer only to cl, not all declared concepts:
        # without their subset evidence they must not guess it.
        result=build_coverage_plan(plan)
        self.assertEqual(len(result['query_plan']['routes']),2)
        exact=topic();exact['concepts']=exact['concepts'][:1]
        normalized=validate_plan(exact)
        for row in normalized['routes']:row.pop('concept_ids')
        self.assertEqual(len(build_coverage_plan(normalized)['query_plan']['routes']),4)
        invalid=validate_plan(topic());invalid['routes'][0]['query']+=' AND infant'
        with self.assertRaises(ValueError):validate_plan(invalid)

    def test_ineligible_filters_identifiers_sources_preserved_and_reported(self):
        plan=topic()
        plan['routes']=[{'id':'limited','source':'pubmed','query':'chloride AND infant[Title]','syntax':'native','limit':100},
            {'id':'known','source':'pubmed','query':'PMID:12345678','limit':1},
            {'id':'other','source':'crossref','query':'chloride','limit':9},topic()['routes'][1]]
        result=build_coverage_plan(plan)
        self.assertEqual(result['query_plan']['routes'][:4],validate_plan(plan)['routes'])
        reasons={r['route_id']:r['reason'] for r in result['skipped_routes']}
        self.assertIn('limited',reasons);self.assertIn('known',reasons)
        self.assertEqual(reasons['other'],'unsupported_source_preserved')
        self.assertEqual(result['budgets']['pubmed']['additional_records'],0)
        self.assertEqual(result['budgets']['europe_pmc']['additional_records'],50)

    def test_multiple_original_routes_share_per_source_addition_not_each50(self):
        plan=topic()
        plan['routes'].append({'id':'pm-duplicate','source':'pubmed','concept_ids':['cl'],'limit':80})
        plan['routes'].append({'id':'pm-kidney','source':'pubmed','concept_ids':['renal'],'limit':70})
        result=build_coverage_plan(plan)
        added=[r for r in result['route_strategies'] if r['strategy']=='native_broad' and r['source']=='pubmed']
        self.assertEqual(len(added),2)
        self.assertEqual([r['limit'] for r in added],[25,25])
        self.assertEqual(result['budgets']['pubmed']['allocated_records'],300)

    def test_existing_equivalent_broad_query_not_duplicated_even_unmarked(self):
        result=build_coverage_plan(topic())['query_plan']
        unmarked={k:v for k,v in result.items() if k!='coverage'}
        repeated=build_coverage_plan(unmarked)
        self.assertEqual(len(repeated['query_plan']['routes']),4)
        self.assertTrue(any(r['reason']=='identical_complementary_query_already_present' for r in repeated['skipped_routes']))

    def test_forged_or_modified_coverage_marker_rejected(self):
        result=build_coverage_plan(topic())['query_plan']
        for mutate in ('quota','origin','ledger','nested','size'):
            plan=deepcopy(result)
            if mutate=='quota':plan['routes'][0]['limit']=99
            elif mutate=='origin':plan['coverage']['original_plan']['question']='changed'
            elif mutate=='ledger':plan['coverage']['budgets']['pubmed']['additional_records']=0
            elif mutate=='nested':plan['coverage']['original_plan']['coverage']={}
            else:plan['coverage']['original_plan']['question']='x'*(2*1024**2)
            with self.subTest(mutate=mutate),self.assertRaises(ValueError):validate_plan(plan)

    def test_fixed_budget_explicit_output_not_automatically_reexpanded(self):
        fixed=build_coverage_plan(topic(),mode='fixed-budget')
        self.assertEqual([r['limit'] for r in fixed['query_plan']['routes']],[50,50,50,50])
        repeated=build_coverage_plan(fixed['query_plan'])
        self.assertEqual(repeated['mode'],'fixed-budget')
        self.assertEqual(repeated['query_plan'],fixed['query_plan'])

    def test_over_budget_fails_before_output_or_requests(self):
        plan=topic();plan['limits']={'total_records':200}
        with tempfile.TemporaryDirectory() as tmp,patch('lit_harvest.pipeline._search_one') as search:
            target=Path(tmp)/'run'
            with self.assertRaisesRegex(ValueError,'original routes were not reduced'):
                pipeline.run_pipeline('topic',config(),1,target,False,env={},query_plan=plan)
            self.assertFalse(target.exists());search.assert_not_called()
        with self.assertRaises(ValueError):build_coverage_plan(topic(),additional_per_source=True)

    def fake(self,calls,fail_append=False):
        def search(source,query,limit,*_a,**_k):
            calls.append((source,query,limit))
            if limit==50 and fail_append:raise ValueError('Appended provider failure')
            if source=='pubmed':
                return [PaperRecord(title='Original chloride patient outcomes',doi='10.1234/original',
                    pmid='12345' if limit==50 else '',authors=['Original Author'],sources=['pubmed'],
                    extra={'screening':{'decision':'include','reason':'Existing reviewed evidence'}})]
            return [PaperRecord(title='Other chloride result '+str(limit),doi='10.1234/epmc'+str(limit),sources=['europe_pmc'])]
        return search

    def test_actual_pipeline_default_retains_original_cid_when_new_id_added(self):
        calls=[]
        original=PaperRecord(title='Original chloride patient outcomes',doi='10.1234/original',authors=['Original Author'],sources=['pubmed'])
        expected=candidate_id(original)
        with tempfile.TemporaryDirectory() as tmp,patch('lit_harvest.pipeline._search_one',side_effect=self.fake(calls)):
            directory,manifest,rows=pipeline.run_pipeline('topic',config(),1,Path(tmp)/'run',False,env={},query_plan=topic())
            self.assertEqual(sorted(limit for _,_,limit in calls),[50,50,100,100])
            inventory=json.loads((directory/'candidates.json').read_text())['records']
            retained=next(r for r in inventory if r['doi']=='10.1234/original')
            self.assertEqual(retained['pmid'],'12345')
            self.assertEqual(retained['extra']['candidate_id'],expected)
            self.assertEqual(retained['extra']['screening']['decision'],'include')
            self.assertEqual(len(rows),1);self.assertEqual(len(inventory),3)
            maps=manifest['source_status']['selection']['original_candidate_mapping']
            entry=next(r for r in maps if r['original_candidate_id']==expected)
            self.assertEqual(entry['merged_candidate_id'],expected)
            self.assertNotEqual(entry['metadata_candidate_id_after_append'],expected)
            pipeline.load_manifest(directory/'manifest.json')

    def test_original_strong_id_conflicts_remain_separate(self):
        plan=build_coverage_plan(topic())['query_plan']
        def search(source,query,limit,*_a,**_k):
            if source!='pubmed':return []
            return [PaperRecord(title='Same specific title of chloride outcomes',doi='10.1234/a' if limit==100 else '10.1234/b',sources=['pubmed'])]
        with patch('lit_harvest.pipeline._search_one',side_effect=search):
            records,status=pipeline.search_all('topic',config(),10,env={},query_plan=plan)
        self.assertEqual(len(records.candidates),2)
        self.assertTrue(all(r.extra.get('identity_conflict') for r in records))

    def test_existing_frozen_cid_pinned_and_duplicate_ids_fail(self):
        def search(source,query,limit,*_a,**_k):
            if source!='pubmed':return []
            return [PaperRecord(title='Frozen original chloride outcome',doi='10.1234/frozen',
                pmid='32145' if limit==50 else '',sources=['pubmed'],
                extra={'candidate_id':'incoming-cid' if limit==50 else 'frozen-cid-not-derived'})]
        with patch('lit_harvest.pipeline._search_one',side_effect=search):
            rows,status=pipeline.search_all('topic',config(),10,env={},query_plan=build_coverage_plan(topic())['query_plan'])
        self.assertEqual(rows[0].extra['candidate_id'],'frozen-cid-not-derived')
        self.assertNotEqual(candidate_id(rows[0]),rows[0].extra['candidate_id'])
        def duplicate(source,query,limit,*_a,**_k):
            if limit==50:return []
            return [PaperRecord(title='Unique '+source,doi='10.1234/'+source,sources=[source],extra={'candidate_id':'duplicated'})]
        with patch('lit_harvest.pipeline._search_one',side_effect=duplicate),self.assertRaisesRegex(ValueError,'Original candidate IDs'):
            pipeline.search_all('topic',config(),10,env={},query_plan=build_coverage_plan(topic())['query_plan'])

    def test_coverage_sidecar_tamper_rejected_and_legacy_without_optional_loads(self):
        calls=[]
        with tempfile.TemporaryDirectory() as tmp,patch('lit_harvest.pipeline._search_one',side_effect=self.fake(calls)):
            directory,manifest,rows=pipeline.run_pipeline('topic',config(),1,Path(tmp)/'run',False,env={},query_plan=topic())
            pipeline.load_manifest(directory/'manifest.json')
            sidecar=directory/'coverage-plan.json'
            sidecar.write_bytes(sidecar.read_bytes()+b' ')
            with self.assertRaisesRegex(ValueError,'coverage-plan.json'):
                pipeline.load_manifest(directory/'manifest.json')
            legacy=deepcopy(manifest);legacy['research_context'].pop('coverage-plan.json')
            path=directory/'legacy-manifest.json';path.write_text(json.dumps(legacy),encoding='utf-8')
            self.assertEqual(len(pipeline.load_manifest(path)[1]),1)

    def cli(self):
        spec=importlib.util.spec_from_file_location('cli_discovery090',SCRIPTS/'literature_harvester.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        return module

    def test_real_cli_default_optout_and_partial_append_exit(self):
        cli=self.cli()
        for name,optout,fail,expected_limits,exitcode in (
            ('default',False,False,[50,50,100,100],0),('original',True,False,[100,100],0),
            ('partial',False,True,[50,50,100,100],2)):
            calls=[]
            with tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);plan=root/'plan.json';plan.write_text(json.dumps(topic()),encoding='utf-8')
                argv=['discover','--plan',str(plan),'--queue-size','1','--output-dir',str(root/name)]
                if optout:argv.append('--original-only')
                with patch('lit_harvest.pipeline._search_one',side_effect=self.fake(calls,fail)),redirect_stdout(io.StringIO()),redirect_stderr(io.StringIO()):
                    code=cli.main(argv)
                self.assertEqual(code,exitcode)
                self.assertEqual(sorted(r[2] for r in calls),expected_limits)
                inventory=json.loads((root/name/'candidates.json').read_text())['records']
                self.assertGreaterEqual(len(inventory),2)

    def test_real_coverage_cli_default_and_explicit_fixed_mode(self):
        cli=self.cli()
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);plan=root/'plan.json';plan.write_text(json.dumps(topic()),encoding='utf-8')
            for mode,total in (('additive',300),('fixed-budget',200)):
                argv=['coverage-plan','--plan',str(plan),'--output-dir',str(root/mode)]
                if mode=='fixed-budget':argv+=['--mode',mode]
                with redirect_stdout(io.StringIO()):self.assertEqual(cli.main(argv),0)
                result=json.loads((root/mode/'query-plan.json').read_text())
                self.assertEqual(sum(r['limit'] for r in result['routes']),total)

    def test_configuration_optout_and_legacy_run_do_not_expand(self):
        cfg=config();cfg['discovery']['additive_default']=False
        validate_config(cfg)
        for planned in (True,False):
            calls=[]
            with tempfile.TemporaryDirectory() as tmp,patch('lit_harvest.pipeline._search_one',side_effect=self.fake(calls)):
                pipeline.run_pipeline('legacy',cfg,1,Path(tmp)/'run',False,env={},query_plan=topic() if planned else None)
            self.assertEqual(len(calls),2)
            self.assertNotIn(50,[r[2] for r in calls])

    def test_original_only_unwraps_verified_plan_and_resume_never_discovers(self):
        calls=[]
        with tempfile.TemporaryDirectory() as tmp,patch('lit_harvest.pipeline._search_one',side_effect=self.fake(calls)):
            root=Path(tmp)
            directory,manifest,rows=pipeline.run_pipeline('topic',config(),1,root/'original',False,env={},
                query_plan=build_coverage_plan(topic())['query_plan'],original_only=True)
            self.assertEqual(sorted(r[2] for r in calls),[100,100])
            with (patch('lit_harvest.pipeline.search_all',side_effect=AssertionError('resume must not discover')),
                  patch('lit_harvest.pipeline.retrieve_population',return_value=None)):
                pipeline.resume_pipeline(directory/'manifest.json',config(),root/'resume',env={})


if __name__=='__main__':unittest.main()
