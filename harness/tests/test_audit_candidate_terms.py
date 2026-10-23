"""Independent witnesses can inspect frozen sum terms without changing costs."""
import copy
import unittest

from test_audit_candidate_checks import example, execute, check_rows, engine


def fixture(scope=None):
    model,baseline,proposal,sources=example()
    scope=scope or {'kind':'element','axis':'sample','multiplicity':2}
    parent=proposal['work_model']['items'][0]
    parent.update(count={'kind':'sum','unit':'MAC','terms':[
        {'kind':'matmul','m':3,'n':1,'k':5,'batch':1,'unit':'MAC'},
        {'kind':'matmul','m':4,'n':1,'k':2,'batch':1,'unit':'MAC'},
        {'kind':'matmul','m':2,'n':1,'k':3,'batch':1,'unit':'MAC'}]},
        scopes={'primary':copy.deepcopy(scope)})
    refs=baseline['computations'][0]['references']
    first={'id':'first','kind':'gemv','input_elements':5,'output_elements':3}
    middle={'id':'middle','kind':'gemv','input_elements':2,'output_elements':4}
    last={'id':'last','kind':'gemv','input_elements':3,'output_elements':2}
    baseline['computations']=[
        {'id':name,'semantics':'independent synthetic operand-shaped effects','steps':steps,
         'scope':copy.deepcopy(scope),'basis':'synthetic source/candidate decomposition differs','references':refs}
        for name,steps in [('ends',[first,last]),('middle',[middle])]]
    row=model['count_checks'][0]
    row.update(source_paths=['ends'],candidate_terms={'arithmetic':[0,2]})
    other=copy.deepcopy(row);other.update(id='middle_count',source_paths=['middle'],candidate_terms={'arithmetic':[1]})
    model['count_checks'].append(other)
    return model,baseline,proposal,sources


class CandidateTermTests(unittest.TestCase):
    def test_different_decompositions_match_disjoint_terms_with_shared_cost_group(self):
        args=fixture();before=copy.deepcopy(args);result=execute(*args)
        self.assertTrue(result['ok'],result['errors']);self.assertEqual(args,before)
        self.assertTrue(result['work_result']['coverage_complete'])
        counts=check_rows(result,'count__')
        self.assertEqual([(row['expected'],row['actual'],row['status']) for row in counts],
                         [(504,504,'pass'),(192,192,'pass')])
        selections=[row['candidate_selection'][0] for row in counts]
        self.assertEqual([row['term_indices'] for row in selections],[[0,2],[1]])
        self.assertTrue(all(row['parent_operations']==696 and not row['full_work_item'] for row in selections))
        self.assertTrue(all(row['scope_trace']['total']==24 and row['unit']=='MAC' for row in selections))
        self.assertTrue(all(row['parent_recipe_sha256'] for row in selections))
        # Counts are source-witness coverage, not complete hardware supply.
        self.assertFalse(result['coverage_complete'])
        self.assertEqual(args[2]['candidates'][0]['cost_groups'][0]['work_ids'],['arithmetic'])
        budget=engine.evaluate(args[2],{key:args[3][key] for key in ('spec.md','reference.py')})
        self.assertEqual(budget['candidates']['small']['ledger']['cost_groups'][0]['operations'],696)

    def test_prefix_and_task_scope_come_from_parent_without_new_repetition(self):
        for scope,total in [({'kind':'prefix','axis':'sample','prefix_axis':'history','first':0,'step':1,'multiplicity':2},132),
                            ({'kind':'task','axis':'run'},1)]:
            with self.subTest(scope=scope):
                result=execute(*fixture(scope));self.assertTrue(result['ok'],result['errors'])
                self.assertEqual([row['actual'] for row in check_rows(result,'count__')],[21*total,8*total])
                self.assertTrue(all(row['candidate_selection'][0]['scope_trace']['total']==total
                                    for row in check_rows(result,'count__')))
        args=fixture({'kind':'task','axis':'run'})
        args[2]['work_model']['items'][0]['count']['terms'][0]['m']='workload_elements'
        args[1]['computations'][0]['steps'][0]['output_elements']='workload_elements'
        result=execute(*args);self.assertTrue(result['ok'],result['errors'])
        self.assertEqual(check_rows(result,'count__')[0]['actual'],66)

    def test_invalid_selectors_reject_locally_without_erasing_other_counts(self):
        invalid=[[],[0,0],[-1],[3],[True],['0'],[{'operations':21}],
                 {'operations':21},None]
        for indices in invalid:
            with self.subTest(indices=indices):
                args=fixture();args[0]['count_checks'][0]['candidate_terms']={'arithmetic':indices}
                result=execute(*args);self.assertFalse(result['ok'])
                bad=next(row for row in result['subreceipts'] if row['id'].startswith('count/0/'))
                self.assertEqual((bad['status'],bad['checks'],bad['values']),('declaration_failed',[],{}))
                self.assertEqual([(row['actual'],row['status']) for row in check_rows(result,'count__')],[(192,'pass')])
        for selectors in ({'missing_parent':[0]},[],None):
            with self.subTest(selectors=selectors):
                args=fixture();args[0]['count_checks'][0]['candidate_terms']=selectors
                result=execute(*args);self.assertFalse(result['ok'])
                self.assertTrue(any('candidate_terms' in error for error in result['errors']))
        args=fixture();args[0]['count_checks'][0].update(candidate_work_ids=['missing_parent'],candidate_terms={'missing_parent':[0]})
        result=execute(*args);self.assertFalse(result['ok'])
        bad=next(row for row in result['subreceipts'] if row['id'].startswith('count/0/'))
        self.assertEqual((bad['status'],bad['checks'],bad['values']),('declaration_failed',[],{}))
        args=fixture();args[2]['work_model']['items'][0]['count']={'kind':'matmul','m':29,'n':1,'k':1,'batch':1,'unit':'MAC'}
        result=execute(*args);self.assertFalse(result['ok'])
        self.assertTrue(any('existing sum work item' in error for error in result['errors']))

    def test_whole_subset_and_subset_subset_overlap_cannot_reuse_credit(self):
        for mode in ('subset_then_whole','whole_then_subset','overlap','all_indices_then_subset'):
            with self.subTest(mode=mode):
                model,baseline,proposal,sources=fixture()
                entire=copy.deepcopy(baseline['computations'][0]);entire['id']='entire'
                entire['steps'].insert(1,copy.deepcopy(baseline['computations'][1]['steps'][0]))
                baseline['computations'].append(entire)
                whole=copy.deepcopy(model['count_checks'][0]);whole.update(id='entire_count',source_paths=['entire'])
                whole.pop('candidate_terms')
                subset=model['count_checks'][0]
                if mode=='subset_then_whole':model['count_checks']=[subset,whole]
                elif mode=='whole_then_subset':model['count_checks']=[whole,subset]
                elif mode=='overlap':model['count_checks'][1]['candidate_terms']={'arithmetic':[2]}
                else:
                    whole['candidate_terms']={'arithmetic':[0,1,2]};model['count_checks']=[whole,subset]
                result=execute(model,baseline,proposal,sources)
                self.assertFalse(result['ok'])
                self.assertTrue(any('duplicate source/candidate count credit' in error for error in result['errors']),result['errors'])
                self.assertEqual(len(check_rows(result,'count__')),1)
                self.assertEqual(check_rows(result,'count__')[0]['status'],'pass')

    def test_whole_item_behavior_and_frozen_recipe_updates_are_preserved(self):
        args=fixture();model,baseline,proposal,sources=args
        baseline['computations'][0]['steps'].insert(1,copy.deepcopy(baseline['computations'][1]['steps'][0]))
        baseline['computations']=baseline['computations'][:1];model['count_checks']=model['count_checks'][:1]
        model['count_checks'][0].pop('candidate_terms')
        result=execute(*args);self.assertTrue(result['ok'],result['errors'])
        check=check_rows(result,'count__')[0]
        self.assertEqual((check['actual'],check['expected'],check['status']),(696,696,'pass'))
        self.assertTrue(check['candidate_selection'][0]['full_work_item'])
        self.assertEqual(check['candidate_selection'][0]['term_indices'],[0,1,2])
        args=fixture();original=execute(*args)
        args[2]['work_model']['items'][0]['count']['terms'][0]['m']=4
        changed=execute(*args)
        original_check=check_rows(original,'count__')[0];changed_check=check_rows(changed,'count__')[0]
        self.assertEqual((changed_check['actual'],changed_check['expected'],changed_check['status']),(624,504,'fail'))
        self.assertNotEqual(changed_check['candidate_selection'][0]['parent_recipe_sha256'],
                            original_check['candidate_selection'][0]['parent_recipe_sha256'])


if __name__=='__main__':unittest.main()
