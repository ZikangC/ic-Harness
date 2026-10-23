"""Lexical source quantities; selection and engineering meaning remain caller-owned."""
import copy
import hashlib
import math
import re


def is_original_source(name):
    """Original-input logical namespace; filenames never imply program authority."""
    if name in ('spec.md', 'reference.py'):
        return True
    if not isinstance(name, str) or not name.startswith('support/'):
        return False
    relative = name[len('support/'):]
    return (bool(relative) and not any(ord(char) < 32 or ord(char) == 127 for char in relative)
            and not any(char in relative for char in ('\\', ':'))
            and all(part not in ('', '.', '..') for part in relative.split('/')))


def original_sources(sources, *, strict=False):
    """Extract the entire declared input closure, rejecting malformed namespaces."""
    if not isinstance(sources, dict) or not {'spec.md', 'reference.py'} <= set(sources):
        raise ValueError('original sources require both spec.md and reference.py')
    selected = {}
    for name, value in sources.items():
        if not isinstance(name, str):
            raise ValueError('source names must be text')
        original = is_original_source(name)
        if not original and (strict or name == 'support' or name.startswith('support/')):
            raise ValueError('invalid original-source name: ' + repr(name))
        if original:
            if not isinstance(value, str):
                raise ValueError('original sources must map filenames to frozen text')
            selected[name] = value
    return selected


_DIGITS = r'\d(?:_?\d)*'
_DECIMAL = r'[-+]?(?:'+_DIGITS+r'(?:\.(?!\.)(?:'+_DIGITS+r')?)?|(?<!\.)\.'+_DIGITS+r')(?:[eE][-+]?'+_DIGITS+r')?'
_TOKEN = re.compile(r"\d+'[hHbBdD][0-9a-fA-F_]+|[-+]?0[xX][0-9a-fA-F_]+|[-+]?0[bB][01_]+|Q\d+\.\d+|" + _DECIMAL)
_AREA = r'(?:mm|um|μm|µm|cm|m)(?:²|\^2|2)'
_STORAGE = r'[kKMGT]?i?(?:bytes|byte|bits|bit|B|b)'
_SIMPLE = r'(?:GHz|MHz|kHz|Hz|tokens|token|items|item|cycles|cycle|ms|us|ns|ps|s|count|dimensionless|周期|字节|位|个|％|%)'
_ATOM = '(?:'+_STORAGE+'|'+_AREA+'|'+_SIMPLE+')'
_UNIT = re.compile(_ATOM+r'(?:\s*/\s*'+_ATOM+r')?(?![A-Za-z0-9_])')
_CARDINAL = {'零': 0, '一': 1, '二': 2, '两': 2, '三': 3, '四': 4, '五': 5, '六': 6, '七': 7, '八': 8, '九': 9, '十': 10}
_ENGLISH = {word: i for i, word in enumerate(('zero','one','two','three','four','five','six','seven','eight','nine','ten'))}
_IDENTIFIER = re.compile(r'[A-Za-z_][A-Za-z_0-9]*(?:\.[A-Za-z_][A-Za-z_0-9]*)*')
_DTYPE = re.compile(r'(?:uint|int|bfloat|float)([1-9][0-9]*)')
_SECTION = re.compile(r'第\s*(\d+(?:\.\d+)*)\s*[章节]|\b(?:section|chapter)\s+(\d+(?:\.\d+)*)\b|^\s*#{1,6}\s+(\d+(?:\.\d+)*)(?=[.\s]|$)', re.I)
_RATE_ENGLISH = re.compile(r'\s*(requests?|transactions?|beats?)(?:\s*/\s*|\s+per\s+)(?:clock\s+)?cycles?\b', re.I)
_RATE_CHINESE_PREFIX = re.compile(r'每(?:个)?(?:时钟)?周期(?:最多|至多)?(?:接受|接收|返回)\s*$')
_RATE_CHINESE_NOUN = re.compile(r'\s*(?:个\s*)?(请求|事务|(?:读\s*)?beat)(?=$|[\s，,；;。.!?、：:])', re.I)


def _rate_unit(line, row):
    """Only adjacent explicit one-cycle units; no clause or rate inference."""
    start, end = row['start_column'] - 1, row['end_column']
    suffix = _RATE_ENGLISH.match(line, end)
    if suffix:
        noun = suffix.group(1).lower().rstrip('s')
        return {'raw': suffix.group(), 'unit': noun+'s/cycle'}
    prefix = _RATE_CHINESE_PREFIX.search(line[:start])
    suffix = _RATE_CHINESE_NOUN.match(line, end)
    if prefix and suffix:
        noun = suffix.group(1).lower()
        unit = 'requests/cycle' if noun == '请求' else 'transactions/cycle' if noun == '事务' else 'beats/cycle'
        return {'raw': line[prefix.start():suffix.end()], 'unit': unit}
    return None


def _syntax_context(line, row):
    """Expose local spelling, not inferred requirement or variable meaning."""
    start, end = row['start_column'] - 1, row['end_column']
    identifiers = list(_IDENTIFIER.finditer(line))
    # A sentence-final dot after an identifier digit is punctuation, unlike
    # the trailing dot of a standalone decimal literal such as ``value=3.``.
    punctuated = next((match for match in identifiers if match.start() <= start < match.end()
                       and end == match.end() + 1 and row['raw'].endswith('.')), None)
    if punctuated:
        row['raw'] = row['raw'][:-1]; row['end_column'] -= 1; end -= 1
        numeric = row['raw'].replace('_', '')
        row['value'] = float(numeric) if 'e' in numeric.lower() else int(numeric)
    enclosing = next((match for match in identifiers
                      if match.start() <= start and end <= match.end()), None)
    if enclosing:
        row['identifier'] = enclosing.group()
        base = enclosing.group().rsplit('.', 1)[-1]
        dtype = _DTYPE.fullmatch(base)
        if dtype and row['value'] == int(dtype.group(1)):
            row['notation'] = 'dtype_width'
            row['dtype'] = enclosing.group()
            row['attached_unit'] = {'raw': base, 'unit': 'bit'}
        elif row['notation'] not in ('fixed_point_component', 'cardinal'):
            row['notation'] = 'identifier_component'
            row['attached_unit'] = None
    neighbors = []
    previous = [match for match in identifiers if match.end() <= start]
    following = [match for match in identifiers if match.start() >= end]
    if previous: neighbors.append(previous[-1].group())
    if following: neighbors.append(following[0].group())
    if neighbors: row['neighbor_identifiers'] = neighbors
    for match in _SECTION.finditer(line):
        if match.group(3) is not None and row['attached_unit']:
            # ``# 9 kHz target`` is an engineering quantity heading, whereas
            # ``# 9. Interface`` has a section-number spelling.
            continue
        if any(a <= start and (end <= b or end == b + 1 and row['raw'].endswith('.')) for a, b in
               (match.span(index) for index in (1, 2, 3) if match.group(index) is not None)):
            row['notation'] = 'section_reference'
            row['attached_unit'] = None
            break


def _unit_after(line, end):
    """Only a directly attached recognized spelling; never infer table/name units."""
    tail = line[end:]
    # A workload spelling such as 9-token contains an attachment separator.
    prefix = re.match(r'\s*(?:-(?=(?:tokens?|items?)(?![A-Za-z0-9_])))?', tail).group()
    match = _UNIT.match(tail, len(prefix))
    if not match:
        return None, end
    if tail[match.end():].lstrip().startswith('/'):
        # Do not bind a known prefix of an unsupported composite unit.
        return None,end
    raw = match.group()
    aliases = {'周期':'cycles', '字节':'byte', '位':'bit', '个':'count', '％':'%'}
    return {'raw': raw, 'unit': aliases.get(raw, raw)}, end + match.end()


def _numbers(line):
    entries, covered = [], []
    for match in _TOKEN.finditer(line):
        start, end = match.span()
        if any(a <= start < b for a,b in covered):
            continue
        raw = match.group()
        if raw.startswith('Q'):
            # Qm.n contains two integer quantities, not a decimal literal.
            for part in re.finditer(r'\d+', raw):
                entries.append({'raw': part.group(), 'value': int(part.group()),
                                'start_column': start+part.start()+1, 'end_column': start+part.end(),
                                'notation': 'fixed_point_component', 'attached_unit': None})
            continue
        if raw.startswith(('-', '+')) and start and (line[start-1].isalnum() or line[start-1] in ')]'):
            start += 1; raw = raw[1:]
        if "'" in raw:
            base_letter, digits = raw.split("'")[1][0], raw.split("'")[1][1:]
            base = {'h':16,'b':2,'d':10}[base_letter.lower()]
            try: value = int(digits.replace('_',''), base)
            except ValueError: continue
            notation = 'encoded_integer'
        elif raw.lstrip('+-').lower().startswith(('0x','0b')):
            value = int(raw.replace('_',''),0); notation = 'encoded_integer'
        else:
            # In a-b the minus is an operator, not the sign of b.
            numeric=raw.replace('_','')
            value = float(numeric) if any(x in numeric.lower() for x in ('.','e')) else int(numeric)
            notation = 'decimal'
        if not math.isfinite(value):
            continue
        attached, unit_end = _unit_after(line, end)
        if start and re.fullmatch(r'[A-Za-z_]',line[start-1]):
            # A following noun is not a suffix digit's unit. Complete dtype
            # spellings receive their width unit later from lexical context.
            attached=None;unit_end=end;notation='identifier_component'
        if attached:
            covered.append((end,unit_end))
        entries.append({'raw': raw, 'value': value, 'start_column': start+1, 'end_column': end,
                        'notation': notation, 'attached_unit': attached})
    # Single cardinal words are lexical candidates only. Restrict Chinese words
    # to explicit resource/count spellings; "二进制" is not a count of two.
    for match in re.finditer(r'[零一二两三四五六七八九十](?=个|路|端口|拍|周期|份)|\b(?:'+ '|'.join(_ENGLISH) +r')\b', line, re.I):
        if any(x['start_column']-1 <= match.start() < x['end_column'] for x in entries):
            continue
        raw = match.group(); value = _CARDINAL[raw] if raw in _CARDINAL else _ENGLISH[raw.lower()]
        attached,_ = _unit_after(line,match.end())
        entries.append({'raw':raw,'value':value,'start_column':match.start()+1,'end_column':match.end(),
                        'notation':'cardinal','attached_unit':attached})
    entries = sorted(entries,key=lambda x:x['start_column'])
    for row in entries:
        explicit_rate = _rate_unit(line, row)
        if explicit_rate: row['attached_unit'] = explicit_rate
        _syntax_context(line, row)
    return entries


def catalogue(sources):
    """Content-bound locations and local syntax, without requirement interpretation."""
    files = {}
    for name, text in sources.items():
        if not isinstance(name,str) or not isinstance(text,str):
            raise ValueError('sources must map filenames to frozen text')
        if (name == 'support' or name.startswith('support/')) and not is_original_source(name):
            raise ValueError('invalid original-source name: ' + repr(name))
        lines = []
        for line_number, line in enumerate(text.splitlines(), 1):
            quantities = _numbers(line)
            if quantities:
                lines.append({'line':line_number,'quantities':[dict(row,number=i) for i,row in enumerate(quantities,1)]})
        files[name] = {'source_sha256':hashlib.sha256(text.encode()).hexdigest(),'lines':lines}
    return {'version':1,'index_basis':'line and number are 1-based; columns are inclusive and 1-based',
            'scope':'lexical literal, explicit adjacent units (including per-cycle units) and dtype width syntax only; dtype describes source representation, not required hardware precision; no requirement meaning or completeness or guaranteed service','files':files}


def view(sources):
    """Small selectors for a prompt; full values and fingerprints remain receipts."""
    data = catalogue(sources); output = ['SOURCE QUANTITY SELECTORS (file,line,number; all 1-based). These are lexical tokens, not requirement interpretations.']
    for name, file in data['files'].items():
        output.append('FILE '+name)
        for row in file['lines']:
            parts = []
            for x in row['quantities']:
                context = ['cols='+str(x['start_column'])+'..'+str(x['end_column']), x['notation']]
                for key in ('identifier', 'dtype'):
                    if key in x: context.append(key+'='+x[key])
                if x.get('neighbor_identifiers'): context.append('near='+','.join(x['neighbor_identifiers']))
                unit = (x['attached_unit']['unit']+'; omit fact.unit' if x['attached_unit']
                        else 'not an engineering fact' if x['notation'] == 'section_reference'
                        else 'unattached; fact.unit required')
                parts.append(str(x['number'])+'='+x['raw']+' ['+unit+'; '+'; '.join(context)+']')
            output.append(str(row['line'])+': '+ '; '.join(parts))
    return '\n'.join(output)


def bind_fact(row, sources, units):
    """Bind exactly the model-selected literal. No search for a better fact."""
    if not isinstance(row,dict) or set(row)-{'id','source','unit'} or not {'id','source'}<=set(row):
        raise ValueError('source fact supports only id, source and optional unit; no authored value/formula/citations')
    selector = row['source']
    if not isinstance(selector,dict) or set(selector)!={'file','line','number'}:
        raise ValueError('source selector requires exactly file,line,number (1-based)')
    if not isinstance(selector['file'],str) or any(type(selector[k]) is not int or selector[k]<1 for k in ('line','number')):
        raise ValueError('source selector file must be text and line/number positive integers')
    if (selector['file'] == 'support' or selector['file'].startswith('support/')) and not is_original_source(selector['file']):
        raise ValueError('invalid original-source name: ' + repr(selector['file']))
    text = sources.get(selector['file'])
    if not isinstance(text,str): raise ValueError('source selector names no supplied frozen file')
    lines = text.splitlines()
    if selector['line']>len(lines): raise ValueError('source selector line is outside frozen file')
    line = lines[selector['line']-1]; rows = _numbers(line)
    if selector['number']>len(rows):
        raise ValueError('source selector number is outside this line; available='+str([dict(number=i,raw=x['raw'],attached_unit=x['attached_unit']) for i,x in enumerate(rows,1)]))
    selected = rows[selector['number']-1]; attached = selected['attached_unit']
    if selected['notation'] == 'section_reference':
        raise ValueError('selected quantity is a section/chapter reference, not an engineering source fact')
    if attached:
        unit = attached['unit']
        if 'unit' in row and units.spelling(row['unit'])!=units.spelling(unit):
            # Spelling aliases are not scale conversion or semantic relabeling.
            # Restrict equivalence to storage suffixes: count/cycle and item/token
            # must not become aliases merely because dimensions happen to match.
            def alias(text):
                text=units.spelling(text)
                return '/'.join(re.sub(r'(?:bit|bits|b)$','b',re.sub(r'(?:byte|bytes|B)$','B',part)) for part in text.split('/'))
            if alias(row['unit'])!=alias(unit):
                raise ValueError('source quantity has attached unit '+unit+'; an override is not a conversion')
        basis = ('explicit dtype suffix width; source representation only, not required hardware precision'
                 if selected['notation'] == 'dtype_width' else 'directly attached source unit')
    else:
        unit = row.get('unit')
        if not isinstance(unit,str) or not unit.strip():
            raise ValueError('source quantity has no attached unit; explicitly declare its semantic unit')
        basis = 'model-declared semantic unit; independent review required'
    fact = {'id':row.get('id'),'value':selected['value'],'unit':unit,
            'citations':[{'file':selector['file'],'start':selector['line'],'end':selector['line'],'quote':line}]}
    receipt = {'fact':row.get('id'),'selector':copy.deepcopy(selector),'source_sha256':hashlib.sha256(text.encode()).hexdigest(),
               'token':copy.deepcopy(selected),'unit_basis':basis,'materialized_fact':copy.deepcopy(fact)}
    if attached and 'unit' in row and units.spelling(row['unit'])!=units.spelling(unit):receipt['declared_unit_alias']=row['unit']
    return fact, receipt
