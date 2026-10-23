import copy
import importlib.util
from pathlib import Path
import unittest


ROOT=Path(__file__).resolve().parents[1]
def module(path,name):
    spec=importlib.util.spec_from_file_location(name,path);result=importlib.util.module_from_spec(spec);spec.loader.exec_module(result);return result
engine=module(ROOT/'skills/microarchitecture-design/scripts/evaluate.py','quantity_calculator')
audit=module(ROOT/'skills/microarchitecture-audit/scripts/validate_review.py','quantity_audit')


def selected(name,line,number,unit=None):
    fact={'id':name,'source':{'file':'spec.md','line':line,'number':number}}
    if unit is not None:fact['unit']=unit
    return fact


class QuantityBindingTests(unittest.TestCase):
    def test_density_attached_unit_and_binary_basis_are_bound(self):
        sources={'spec.md':'Density uses binary 3 Mbit/mm²; area is an estimate.'}
        raw={'facts':[selected('density',1,1)],'constraints':[],'runtime_parameters':[],
             'storage_area_model':{'status':'supplied','density_fact':'density'}}
        saved=copy.deepcopy(raw);prepared=engine.prepare_requirements(raw,sources)
        self.assertEqual(raw,saved);self.assertEqual(prepared['errors'],[])
        values,errors=engine.validate_requirements(prepared['requirements'],sources)
        self.assertEqual(errors,[]);self.assertEqual(values['density'],3)
        model=engine._units.macro_density(prepared['requirements'],values)
        self.assertEqual(model['bits_per_mm2'],3*2**20)
        self.assertEqual(prepared['bindings'][0]['unit_basis'],'directly attached source unit')

    def test_source_unit_cannot_be_overridden_as_conversion(self):
        report=engine.prepare_requirements({'facts':[selected('clock',1,1,'Hz')]},{'spec.md':'Target: 9 kHz'})
        self.assertTrue(any('override is not a conversion' in e for e in report['errors']))

    def test_unit_without_literal_attachment_remains_declared_semantics(self):
        report=engine.prepare_requirements({'facts':[selected('rows',1,2,'items')]},{'spec.md':'shape is 7 by 5'})
        self.assertEqual(report['errors'],[]);self.assertEqual(report['requirements']['facts'][0]['value'],5)
        self.assertIn('independent review',report['bindings'][0]['unit_basis'])
        bad=engine.prepare_requirements({'facts':[selected('rows',1,2)]},{'spec.md':'shape is 7 by 5'})
        self.assertTrue(any('no attached unit' in e for e in bad['errors']))

    def test_no_search_or_default_for_wrong_selector(self):
        for line,number in [(2,1),(1,2),(0,1),(1,True)]:
            with self.subTest(line=line,number=number):
                row=selected('rows',line,number,'count')
                report=engine.prepare_requirements({'facts':[row]},{'spec.md':'rows: 7'})
                self.assertTrue(report['errors']);self.assertEqual(report['requirements']['facts'][0],row)

    def test_conversion_is_fixed_and_order_independent(self):
        sources={'spec.md':'Target clock 9 kHz'}
        raw={'facts':[{'id':'hz','convert':{'fact':'clock','to_unit':'Hz'}},selected('clock',1,1)],'constraints':[],'runtime_parameters':[]}
        prepared=engine.prepare_requirements(raw,sources)
        self.assertEqual(prepared['errors'],[])
        values,errors=engine.validate_requirements(prepared['requirements'],sources)
        self.assertEqual(errors,[]);self.assertEqual(values['hz'],9000)
        converted=prepared['requirements']['facts'][0]
        self.assertEqual(converted['converted_from'],{'fact':'clock','to_unit':'Hz'})
        self.assertEqual(converted['citations'],prepared['requirements']['facts'][1]['citations'])

    def test_materialized_conversion_tampering_is_rejected_not_repaired(self):
        raw={'facts':[selected('clock',1,1),{'id':'hz','convert':{'fact':'clock','to_unit':'Hz'}}]}
        sources={'spec.md':'9 kHz'};materialized=engine.prepare_requirements(raw,sources)['requirements']
        materialized['facts'][1]['value']=9;before=copy.deepcopy(materialized)
        report=engine.prepare_requirements(materialized,sources,declaration_mode=False)
        self.assertTrue(any('differs from fixed recalculation' in e for e in report['errors']))
        self.assertEqual(materialized,before);self.assertEqual(report['requirements']['facts'][1]['value'],9)

    def test_conversion_chains_keep_density_prefix(self):
        raw={'facts':[selected('density',1,1),{'id':'bits','convert':{'fact':'density','to_unit':'bit/mm2'}},
                      {'id':'back','convert':{'fact':'bits','to_unit':'Mbit/mm2'}}], 'constraints':[],'runtime_parameters':[]}
        values,errors=engine.validate_requirements(raw,{'spec.md':'binary 3 Mbit/mm²'})
        self.assertEqual(errors,[]);self.assertEqual(values['bits'],3*2**20);self.assertEqual(values['back'],3)

    def test_lexical_ranges_fixed_point_hex_percent_and_negative(self):
        source='legal 7..19; int4 Q2.10; -3.5; 0x2A; 12%'
        rows=engine.source_quantities({'spec.md':source})['files']['spec.md']['lines'][0]['quantities']
        self.assertEqual([x['value'] for x in rows],[7,19,4,2,10,-3.5,42,12])
        self.assertEqual(rows[-1]['attached_unit']['unit'],'%')
        self.assertEqual([x['number'] for x in rows],list(range(1,len(rows)+1)))

    def test_density_unit_exponent_is_not_an_extra_selector(self):
        rows=engine.source_quantities({'spec.md':'density 3 Mbit/mm^2; limit 7 mm2'})['files']['spec.md']['lines'][0]['quantities']
        self.assertEqual([x['value'] for x in rows],[3,7])
        self.assertEqual(rows[0]['attached_unit']['unit'],'Mbit/mm^2')

    def test_python_digit_separators_and_workload_unit_before_chinese(self):
        rows=engine.source_quantities({'spec.md':'9-token任务；rate=12_500 Hz; eps=1.2_5e-3'})['files']['spec.md']['lines'][0]['quantities']
        self.assertEqual([x['value'] for x in rows],[9,12500,0.00125])
        self.assertEqual(rows[0]['attached_unit']['unit'],'token')
        facts=[selected('workload',1,1),selected('rate',1,2),selected('eps',1,3,'dimensionless')]
        values,errors=engine.validate_requirements({'facts':facts,'constraints':[],'runtime_parameters':[]},{'spec.md':'9-token任务；rate=12_500 Hz; eps=1.2_5e-3'})
        self.assertEqual(errors,[]);self.assertEqual(values['rate'],12500)

    def test_encoded_hex_literal_binding_validates_without_decimal_copy(self):
        values,errors=engine.validate_requirements({'facts':[selected('command',1,1,'count')],'constraints':[],'runtime_parameters':[]},{'spec.md':'command = 0x2A'})
        self.assertEqual(errors,[]);self.assertEqual(values['command'],42)

    def test_signed_encoded_literal_and_composite_cost_units(self):
        rows=engine.source_quantities({'spec.md':'-0x20; x-0b10; cost 7 cycles/byte; 3 cycles/word'})['files']['spec.md']['lines'][0]['quantities']
        self.assertEqual([x['value'] for x in rows],[-32,2,7,3])
        self.assertEqual(rows[2]['attached_unit']['unit'],'cycles/byte')
        self.assertIsNone(rows[3]['attached_unit'])

    def test_encoding_identifier_component_does_not_take_following_noun_as_unit(self):
        sources={'spec.md':'Input is uint24 token ID; process 9 tokens.'}
        rows=engine.source_quantities(sources)['files']['spec.md']['lines'][0]['quantities']
        self.assertEqual(rows[0]['value'],24);self.assertEqual(rows[0]['attached_unit']['unit'],'bit')
        self.assertEqual(rows[0]['notation'],'dtype_width');self.assertEqual(rows[0]['dtype'],'uint24')
        self.assertEqual(rows[1]['attached_unit']['unit'],'tokens')
        raw={'facts':[selected('encoding_width',1,1)],'constraints':[],'runtime_parameters':[]}
        values,errors=engine.validate_requirements(raw,sources)
        self.assertEqual(errors,[]);self.assertEqual(values['encoding_width'],24)
        for invalid in ('token','count','byte'):
            self.assertTrue(engine.prepare_requirements({'facts':[selected('width',1,1,invalid)]},sources)['errors'])

    def test_source_content_fingerprint_changes_without_guessing_semantics(self):
        a=engine.source_quantities({'spec.md':'7 rows'})
        b=engine.source_quantities({'spec.md':'7 columns'})
        self.assertNotEqual(a['files']['spec.md']['source_sha256'],b['files']['spec.md']['source_sha256'])
        first=a['files']['spec.md']['lines'][0]['quantities'][0]
        second=b['files']['spec.md']['lines'][0]['quantities'][0]
        self.assertEqual(first['value'],second['value']);self.assertEqual(first['notation'],second['notation'])
        self.assertEqual(first['neighbor_identifiers'],['rows']);self.assertEqual(second['neighbor_identifiers'],['columns'])

    def test_complete_dtype_spellings_bind_width_without_hardware_precision_claim(self):
        sources={'spec.md':'Input uint48; arithmetic int40; source np.float80 and bfloat16.'}
        raw={'facts':[selected(name,1,number) for number,name in enumerate(('u','i','f','b'),1)],
             'constraints':[],'runtime_parameters':[]}
        prepared=engine.prepare_requirements(raw,sources)
        self.assertEqual(prepared['errors'],[])
        values,errors=engine.validate_requirements(prepared['requirements'],sources)
        self.assertEqual(errors,[]);self.assertEqual(values,{'u':48,'i':40,'f':80,'b':16})
        self.assertEqual([fact['unit'] for fact in prepared['requirements']['facts']],['bit']*4)
        self.assertEqual([binding['token']['dtype'] for binding in prepared['bindings']],['uint48','int40','np.float80','bfloat16'])
        self.assertTrue(all('source representation only' in binding['unit_basis'] for binding in prepared['bindings']))
        self.assertNotIn('internal_precision',prepared['requirements'])

    def test_generic_identifiers_are_not_dtype_widths_or_following_units(self):
        sources={'spec.md':'print48; my_uint48 token; float80_x; kernel3; 11 rows'}
        rows=engine.source_quantities(sources)['files']['spec.md']['lines'][0]['quantities']
        self.assertEqual([row['value'] for row in rows],[48,48,80,3,11])
        self.assertEqual([row['identifier'] for row in rows[:4]],['print48','my_uint48','float80_x','kernel3'])
        self.assertTrue(all(row['notation']=='identifier_component' and row['attached_unit'] is None and 'dtype' not in row for row in rows[:4]))
        raw={'facts':[selected('kernel_number',1,4,'count'),selected('row_count',1,5,'count')],
             'constraints':[],'runtime_parameters':[]}
        values,errors=engine.validate_requirements(raw,sources)
        self.assertEqual(errors,[]);self.assertEqual(values,{'kernel_number':3,'row_count':11})

    def test_chapter_references_cannot_be_bound_but_selector_order_remains(self):
        sources={'spec.md':'见第 6 节，输出 uint48；Section 13 requires 11 rows.\n## 9. Capacity\nChapter 17: state'}
        rows=engine.source_quantities(sources)['files']['spec.md']['lines'][0]['quantities']
        self.assertEqual([row['value'] for row in rows],[6,48,13,11])
        self.assertEqual([row['number'] for row in rows],[1,2,3,4])
        self.assertEqual(rows[0]['notation'],'section_reference');self.assertEqual(rows[2]['notation'],'section_reference')
        for line,number in ((1,1),(1,3),(2,1),(3,1)):
            with self.subTest(line=line,number=number):
                raw={'facts':[selected('fake_width',line,number,'bit')]}
                report=engine.prepare_requirements(raw,sources)
                self.assertTrue(any('section/chapter reference' in error for error in report['errors']))
                self.assertEqual(report['requirements'],raw)
        values,errors=engine.validate_requirements({'facts':[selected('width',1,2),selected('rows',1,4,'count')],
                                                   'constraints':[],'runtime_parameters':[]},sources)
        self.assertEqual(errors,[]);self.assertEqual(values,{'width':48,'rows':11})

    def test_ordinal_counts_cardinals_and_section_variable_are_not_chapter_refs(self):
        sources={'spec.md':'第3个lane，第5拍；table: 7 rows; section_count = 11; one count; 二周期'}
        rows=engine.source_quantities(sources)['files']['spec.md']['lines'][0]['quantities']
        self.assertEqual([row['value'] for row in rows],[3,5,7,11,1,2])
        self.assertTrue(all(row['notation']!='section_reference' for row in rows))
        facts=[selected('n'+str(number),1,number, None if rows[number-1]['attached_unit'] else 'count')
               for number in range(1,len(rows)+1)]
        values,errors=engine.validate_requirements({'facts':facts,'constraints':[],'runtime_parameters':[]},sources)
        self.assertEqual(errors,[]);self.assertEqual(list(values.values()),[3,5,7,11,1,2])

    def test_quantity_heading_is_not_a_chapter_number(self):
        sources={'spec.md':'# 9 kHz target\n## 11 mm² area limit\n# 13. Interface'}
        raw={'facts':[selected('clock',1,1),selected('area',2,1)],'constraints':[],'runtime_parameters':[]}
        values,errors=engine.validate_requirements(raw,sources)
        self.assertEqual(errors,[]);self.assertEqual(values,{'clock':9,'area':11})
        rows=engine.source_quantities(sources)['files']['spec.md']['lines']
        self.assertEqual([row['quantities'][0]['notation'] for row in rows],['decimal','decimal','section_reference'])
        self.assertTrue(engine.prepare_requirements({'facts':[selected('fake_width',3,1,'bit')]},sources)['errors'])

    def test_context_columns_cover_complete_identifier_and_chinese_adjacent_dtype(self):
        sources={'spec.md':'类型uint48，超长标识符_long_identifier_for_width=11; Section 13'}
        rows=engine.source_quantities(sources)['files']['spec.md']['lines'][0]['quantities']
        line=sources['spec.md']
        self.assertEqual(rows[0]['identifier'],'uint48');self.assertEqual(rows[0]['dtype'],'uint48')
        self.assertIn('_long_identifier_for_width',rows[1]['neighbor_identifiers'])
        for row in rows:
            self.assertEqual(line[row['start_column']-1:row['end_column']],row['raw'])
        view=engine.quantity_view(sources)
        self.assertIn('cols='+str(rows[0]['start_column'])+'..'+str(rows[0]['end_column']),view)
        self.assertIn('dtype=uint48',view);self.assertIn('_long_identifier_for_width',view)
        self.assertIn('section_reference',view)

    def test_source_and_literal_or_conversion_conflict_are_rejected(self):
        row=selected('a',1,1,'count');row['value']=7
        self.assertTrue(engine.prepare_requirements({'facts':[row]},{'spec.md':'7'})['errors'])
        raw={'facts':[selected('clock',1,1),{'id':'hz','convert':{'fact':'clock','to_unit':'Hz'},'value':9000}]}
        self.assertTrue(engine.prepare_requirements(raw,{'spec.md':'9 kHz'})['errors'])

    def test_raw_literal_and_materialized_conversion_fields_cannot_bypass_binding(self):
        raw={'facts':[{'id':'rows','value':7,'unit':'count','citations':[{'file':'spec.md','start':1,'end':1,'quote':'7'}]}]}
        self.assertTrue(engine.prepare_requirements(raw,{'spec.md':'7'})['errors'])
        self.assertEqual(engine.validate_requirements(dict(raw,constraints=[],runtime_parameters=[]),{'spec.md':'7'})[1],[])

    def test_auditor_uses_own_source_choices_and_conversion(self):
        sources={'spec.md':'work: 7 by 5; target 9 kHz'}
        request={'tool':'recompute','facts':[selected('m',1,1,'count'),selected('k',1,2,'count'),selected('clock',1,3),
                                          {'id':'hz','convert':{'fact':'clock','to_unit':'Hz'}}],
                 'assumptions':[], 'calculations':[{'id':'ops','formula':'m*k','unit':'MAC','basis':'independent source shape reconstruction'},
                                                 {'id':'period','formula':'1/hz','unit':'s','basis':'converted clock reciprocal'}]}
        before=copy.deepcopy(request);result=audit.recompute(request,sources,engine)
        self.assertEqual(request,before);self.assertTrue(result['ok'],result['errors']);self.assertEqual(result['values']['ops'],35)
        self.assertAlmostEqual(result['values']['period'],1/9000)
        self.assertEqual(len(result['citation_bindings']),4)

    def test_explicit_adjacent_per_cycle_units_bind_without_model_semantic_override(self):
        cases=[('峰值每周期接受三个请求、返回两个读 beat。',3,'requests/cycle'),
               ('每个时钟周期至多接收五个事务。',5,'transactions/cycle'),
               ('每时钟周期返回两个读 beat。',2,'beats/cycle'),
               ('The port accepts 3 requests per cycle.',3,'requests/cycle'),
               ('The port accepts three transactions per clock cycle.',3,'transactions/cycle'),
               ('Peak return rate: 7 beats/cycle.',7,'beats/cycle')]
        for text,value,unit in cases:
            with self.subTest(text=text):
                sources={'spec.md':text};declaration={'facts':[selected('rate',1,1)],'constraints':[],'runtime_parameters':[]}
                before=copy.deepcopy(declaration)
                prepared=engine.prepare_requirements(declaration,sources)
                self.assertEqual(prepared['errors'],[]);self.assertEqual(declaration,before)
                fact=prepared['requirements']['facts'][0]
                self.assertEqual((fact['value'],fact['unit']),(value,unit))
                self.assertEqual(fact['citations'][0]['quote'],text)
                values,errors=engine.validate_requirements(prepared['requirements'],sources)
                self.assertEqual(errors,[]);self.assertEqual(values['rate'],value)
                # These semantic rate names need no unit-conversion vocabulary.
                self.assertIsNone(engine._units.unit(unit))
                self.assertTrue(engine.prepare_requirements({'facts':[selected('rate',1,1,'count')]},sources)['errors'])
                self.assertTrue(engine.prepare_requirements({'facts':[selected('rate',1,1,'bytes/cycle')]},sources)['errors'])

    def test_rate_lexing_preserves_quantity_selectors_and_does_not_inherit_scope(self):
        text='width 40 bits; alignment 6B; 峰值每周期接受三个请求、返回两个读 beat；queue 11 items.'
        sources={'spec.md':text};rows=engine.source_quantities(sources)['files']['spec.md']['lines'][0]['quantities']
        self.assertEqual([x['raw'] for x in rows],['40','6','三','两','11'])
        self.assertEqual([x['number'] for x in rows],[1,2,3,4,5])
        self.assertEqual(rows[2]['attached_unit']['unit'],'requests/cycle')
        self.assertEqual(rows[3]['attached_unit']['unit'],'count')
        for row in rows:self.assertEqual(text[row['start_column']-1:row['end_column']],row['raw'])
        self.assertEqual(rows[3]['value'],2)
        self.assertTrue(engine.prepare_requirements({'facts':[selected('inherited',1,4,'beats/cycle')]},sources)['errors'])
        receipt=engine.prepare_requirements({'facts':[selected('peak',1,3)]},sources)['bindings'][0]
        self.assertEqual(receipt['selector'],{'file':'spec.md','line':1,'number':3})
        self.assertNotIn('guaranteed_ready',receipt)

    def test_multicycle_denominators_separate_clauses_and_identifier_suffixes_are_not_rates(self):
        cases=[('每两个周期接受三个请求。','三'),
               ('The port accepts 3 requests per 2 cycles.','3'),
               ('The port accepts 3 requests per several cycles.','3'),
               ('Accepts 3 requests. Per cycle, 5 bytes are transferred.','3'),
               ('每周期统计字节数，接受三个请求。','三'),
               ('每周期接受三个请求，每四个周期返回两个beat。','两'),
               ('每周期接受三个请求；队列中有五个请求。','五'),
               ('每周期接受三个请求队列。','三'),
               ('kernel3 requests per cycle.','3')]
        for text,raw in cases:
            with self.subTest(text=text):
                row=next(x for x in engine.source_quantities({'spec.md':text})['files']['spec.md']['lines'][0]['quantities'] if x['raw']==raw)
                self.assertNotIn((row.get('attached_unit') or {}).get('unit'),('requests/cycle','transactions/cycle','beats/cycle'))
        # Source types remain representation widths, even next to a rate noun.
        sources={'spec.md':'uint40 requests per cycle.'}
        fact=engine.prepare_requirements({'facts':[selected('width',1,1)]},sources)['requirements']['facts'][0]
        self.assertEqual((fact['value'],fact['unit']),(40,'bit'))
        sources={'spec.md':'# 7 requests per cycle target'}
        self.assertEqual(engine.prepare_requirements({'facts':[selected('rate',1,1)]},sources)['errors'],[])


if __name__=='__main__':unittest.main()

class StorageSpellingAliasTests(unittest.TestCase):
    def test_alias_is_same_value_not_prefix_or_semantic_change(self):
        import importlib.util
        from pathlib import Path
        path=Path(__file__).resolve().parents[1]/'skills/microarchitecture-design/scripts/evaluate.py'
        spec=importlib.util.spec_from_file_location('alias_calc',path);e=importlib.util.module_from_spec(spec);spec.loader.exec_module(e)
        sources={'spec.md':'size 512 bits\nsize 3 kbytes\n'}
        req={'facts':[{'id':'bits','source':{'file':'spec.md','line':1,'number':1},'unit':'bit'},
                      {'id':'bytes','source':{'file':'spec.md','line':2,'number':1},'unit':'kB'}], 'constraints':[],'runtime_parameters':[]}
        r=e.prepare_requirements(req,sources);self.assertEqual(r['errors'],[])
        self.assertEqual(r['requirements']['facts'][0]['value'],512)
        self.assertEqual(r['bindings'][0]['declared_unit_alias'],'bit')
        for invalid in ['bytes','kbit','cycles','count']:
            req['facts'][0]['unit']=invalid
            self.assertTrue(e.prepare_requirements(req,sources)['errors'])
