"""Deterministic frozen-text locations; no requirement inference or quote selection."""
import hashlib
import json
from harness.runtime.run_store import dump
from harness.runtime.input_bundle import INPUTS, is_original_source, original_sources


def fingerprint(text):
    return hashlib.sha256(text.encode()).hexdigest()


def citation(file, text, start, end, quote, source_hash=None):
    row = {'file':file, 'source_sha256':source_hash or fingerprint(text),
           'start':start, 'end':end, 'quote':quote}
    row['evidence_id'] = hashlib.sha256(json.dumps(row, sort_keys=True, ensure_ascii=False,
                                                separators=(',', ':')).encode()).hexdigest()
    return row


def selected_span(reference,sources,input_only=False):
    if not isinstance(reference,dict) or set(reference)!={'file','start','end'}:raise ValueError('selected span requires file, start and end only')
    file=reference['file'];a,b=reference['start'],reference['end']
    if not isinstance(file,str) or file not in sources or (input_only and not is_original_source(file)):raise ValueError('selected span is outside frozen source scope')
    text=sources[file];lines=text.splitlines(keepends=True)
    if type(a) is not int or type(b) is not int or not 1<=a<=b<=len(lines):raise ValueError('selected span needs integer bounds within frozen source '+file+'; requested='+repr((a,b))+'; valid=1..'+str(len(lines)))
    quote=''.join(lines[a-1:b]).rstrip('\r\n')
    if not quote:raise ValueError('selected span has no nonempty excerpt')
    return citation(file,text,a,b,quote)


def catalogue(sources, input_only=False):
    selected = original_sources(sources) if input_only else dict(sources)
    citations = []
    hashes={}
    for name in sorted(selected):
        text = selected[name]
        if not isinstance(text, str):
            raise ValueError('evidence source must be frozen text')
        hashes[name]=fingerprint(text)
        for number, line in enumerate(text.splitlines(), 1):
            if line.strip():
                citations.append(citation(name, text, number, number, line,hashes[name]))
    return {'source_sha256':hashes,'line_counts':{name:len(text.splitlines()) for name,text in selected.items()},
            'citations':citations,
            'scope':'all nonempty frozen source lines; model selects evidence and verifies meaning'}


def checkpoint(out, label, sources, input_only=False):
    expected = catalogue(sources, input_only)
    version = hashlib.sha256(json.dumps(expected,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
    path = out/(label+'.'+version+'.json')
    if path.exists():
        saved = json.loads(path.read_text())
        if saved != expected:
            raise ValueError('source evidence checkpoint differs from frozen text')
        return saved
    dump(path, expected)
    return expected


def prompt_catalogue(record):
    # The same prompt already contains numbered source text. Do not duplicate
    # every excerpt and JSON key in a second large block.
    lines=['\nFROZEN SOURCE LOCATIONS (choose against numbered originals; prefer {"file":"spec.md","start":1,"end":2}; choose the actual relevant lines, this is syntax only). The checker extracts exactly this range and records its original text/content-bound ID; no need to copy hashes or quotations.']
    for file,source_hash in record['source_sha256'].items():
        count=record.get('line_counts',{}).get(file)
        lines.append('FILE '+file+' lines='+str(count)+' source_sha256='+source_hash+' (fingerprint is program metadata; do not transcribe it)')
    return '\n'.join(lines)
