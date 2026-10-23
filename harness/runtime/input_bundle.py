"""Explicit UTF-8 input closure; preserve sources without executing Python."""
import ast
import json
from pathlib import Path, PurePosixPath

INPUTS = ('spec.md', 'reference.py')


def safe_relative_path(name):
    return (isinstance(name,str) and bool(name) and '\\' not in name and ':' not in name
            and not any(ord(char)<32 or ord(char)==127 for char in name)
            and not PurePosixPath(name).is_absolute()
            and all(part not in ('','.','..') for part in name.split('/')))


def is_original_source(name):
    return name in INPUTS or (isinstance(name,str) and name.startswith('support/')
                              and safe_relative_path(name[len('support/'):]))


def original_sources(sources,strict=False):
    if not isinstance(sources,dict) or not all(name in sources for name in INPUTS):
        raise ValueError('original sources require spec.md and reference.py')
    selected={}
    for name,text in sources.items():
        if isinstance(name,str) and (name=='support' or name.startswith('support/')) and not is_original_source(name):
            raise ValueError('invalid original support source name: '+name)
        if is_original_source(name):
            if not isinstance(text,str):raise ValueError('original source must be UTF-8 text: '+str(name))
            selected[name]=text
        elif strict:
            raise ValueError('non-input source in original source bundle: '+str(name))
    return selected


def _manifest_object(pairs):
    result={}
    for key,value in pairs:
        if key in result:raise ValueError('duplicate input manifest field: '+key)
        result[key]=value
    return result


def load_input_bundle(directory):
    root=Path(directory).resolve();manifest=root/'sources.json'
    manifest_bytes=None
    if manifest.exists() or manifest.is_symlink():
        # The manifest is metadata, never a selectable semantic source.
        manifest.resolve().relative_to(root)
        if not manifest.is_file():raise ValueError('sources.json must be an existing regular file')
        manifest_bytes=manifest.read_bytes()
        declaration=json.loads(manifest_bytes.decode('utf-8'),object_pairs_hook=_manifest_object)
        if (not isinstance(declaration,dict) or set(declaration)!={'schema_version','spec','reference','support'}
                or type(declaration['schema_version']) is not int or declaration['schema_version']!=1):
            raise ValueError('sources.json requires schema_version=1, spec, reference and support only')
        support=declaration['support']
        if not isinstance(support,list) or not all(isinstance(name,str) for name in support) or len(support)!=len(set(support)):
            raise ValueError('sources.json support must be an array of unique relative paths')
        mapping={'spec.md':declaration['spec'],'reference.py':declaration['reference']}
        if declaration['reference']!='reference.py':support=[declaration['reference'],*support]
        for name in support:
            if not safe_relative_path(name):raise ValueError('input source requires a canonical relative path: '+str(name))
            mapping['support/'+name]=name
    else:
        mapping={name:name for name in INPUTS}
    sources={};ast_nodes={};cache={}
    for logical,relative in mapping.items():
        if not safe_relative_path(relative):raise ValueError('input source requires a canonical relative path: '+str(relative))
        path=(root/relative).resolve()
        try:path.relative_to(root)
        except ValueError as error:raise ValueError('input source escapes input directory: '+relative) from error
        if path==manifest.resolve():raise ValueError('sources.json cannot be an input source')
        if not path.is_file():raise ValueError('input source must be an existing regular file: '+relative)
        if path not in cache:cache[path]=path.read_bytes().decode('utf-8')
        text=cache[path];sources[logical]=text
        if path.suffix=='.py' or logical=='reference.py':
            tree=ast.parse(text,filename=relative)
            compile(tree,relative,'exec')  # Syntax check only; never execute.
            ast_nodes[logical]=sum(1 for _ in ast.walk(tree))
    if not sources['spec.md'].strip():raise ValueError('empty spec')
    original_sources(sources,strict=True)
    files={'input/'+name:text.encode('utf-8') for name,text in sources.items()}
    if manifest_bytes is not None:files['input/sources.json']=manifest_bytes
    return sources,files,{'source_paths':mapping,'python_ast_nodes':ast_nodes['reference.py'],
                          'python_ast_nodes_by_source':ast_nodes}
