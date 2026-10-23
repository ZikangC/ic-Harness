"""Integer ABI geometry over explicit declarations; no codec or RTL inference."""
import math


class UnsupportedGeometry(ValueError):
    pass


def _id(value):
    if not isinstance(value,str) or not value.isidentifier():raise ValueError('geometry IDs must be identifiers')
    return value


def _ceildiv(value,divisor):
    return (value+divisor-1)//divisor


def derive(row,names,expression):
    """One fixed recipe. All outputs are integers and probes are finite witnesses."""
    values={};details={};probes={}
    def number(value,lower=0):
        value=expression(value,names)
        if type(value) not in (int,float) or not math.isfinite(value) or value!=int(value) or value<lower:
            raise ValueError('finite integer >= '+str(lower)+' required')
        return int(value)
    def rows(value):
        if not isinstance(value,list) or len(value)>4096:raise ValueError('bounded geometry array required')
        return value
    def put(key,value):
        if key in values:raise ValueError('duplicate geometry output '+key)
        values[key]=value
    def query_id(query):
        pid=_id(query['id'])
        if pid in probes:raise ValueError('duplicate geometry probe '+pid)
        return pid
    try:
        if not isinstance(row,dict):raise ValueError('geometry must be an object')
        _id(row['id']);kind=row['kind'];metadata={'id','kind','basis','references'}
        allowed={
            'region':metadata|{'fields','beat_bytes'},
            'axis_pack':metadata|{'shape','axis','element_bits','carrier_bits','order','slot_order','probes'},
            'beat_span':metadata|{'payload_bytes','beat_bytes','probes'},
        }
        if kind not in allowed:raise UnsupportedGeometry('unsupported geometry kind')
        if set(row)-allowed[kind]:raise ValueError('unknown geometry fields: '+str(sorted(set(row)-allowed[kind])))
        if kind=='region':
            beat=number(row['beat_bytes'],1);cursor=0;payload=0;seen=set();fields=[];intervals=[]
            for field in rows(row['fields']):
                if not isinstance(field,dict) or set(field)!={'id','payload_bytes','alignment_bytes'}:
                    raise ValueError('region field requires id, payload_bytes and alignment_bytes')
                fid=_id(field['id'])
                if fid in seen:raise ValueError('duplicate region field '+fid)
                seen.add(fid);size=number(field['payload_bytes']);alignment=number(field['alignment_bytes'],1)
                start=_ceildiv(cursor,alignment)*alignment;end=start+size
                fields.append({'id':fid,'offset_bytes':start,'payload_bytes':size,'end_bytes':end,'padding_before_bytes':start-cursor})
                for key,value in fields[-1].items():
                    if key!='id':put('f_'+fid+'__'+key,value)
                if size:intervals.append((start//beat,_ceildiv(end,beat)))
                payload+=size;cursor=end
            union=[]
            for start,end in sorted(intervals):
                if union and start<=union[-1][1]:union[-1]=(union[-1][0],max(end,union[-1][1]))
                else:union.append((start,end))
            beats=sum(end-start for start,end in union)
            values.update(payload_bytes=payload,extent_bytes=cursor,contiguous_wire_bytes=_ceildiv(cursor,beat)*beat,
                          contiguous_beats=_ceildiv(cursor,beat),field_beats_lower_bound=beats,field_wire_bytes_lower_bound=beats*beat)
            details={'fields':fields,'field_beat_intervals':union,
                     'scope':'relative aligned-base layout; contiguous read covers the region, field beat union is only a necessary lower bound, not a transfer schedule'}
        elif kind=='axis_pack':
            if row.get('order','C')!='C' or row.get('slot_order','lsb_first')!='lsb_first':
                raise UnsupportedGeometry('axis_pack supports C-order and lsb_first slots only')
            shape=[number(value,1) for value in rows(row['shape'])]
            if not shape or len(shape)>32:raise ValueError('shape needs 1..32 positive extents')
            axis=number(row['axis']);element=number(row['element_bits'],1);carrier=number(row['carrier_bits'],1)
            if axis>=len(shape):raise ValueError('packed axis outside shape')
            if carrier%8 or element>carrier:raise UnsupportedGeometry('axis_pack needs byte-sized carriers containing whole elements')
            slots=carrier//element;packed=list(shape);packed[axis]=_ceildiv(shape[axis],slots)
            carriers=math.prod(packed)
            values.update(slots_per_carrier=slots,packed_axis_extent=packed[axis],carrier_count=carriers,
                          packed_bytes=carriers*(carrier//8),padding_bits_per_carrier=carrier-slots*element)
            for query in rows(row.get('probes',[])):
                pid=query_id(query);forward=set(query)=={'id','logical_index'};inverse=set(query)=={'id','packed_index','lane'}
                if not forward and not inverse:raise ValueError('probe needs logical_index OR packed_index/lane')
                index=[number(value) for value in rows(query['logical_index'] if forward else query['packed_index'])];input_index=list(index)
                bounds=shape if forward else packed
                if len(index)!=len(bounds) or any(value>=bound for value,bound in zip(index,bounds)):
                    raise ValueError('geometry probe index outside shape')
                if forward:
                    logical=list(index);lane=index[axis]%slots;index[axis]//=slots
                else:
                    lane=number(query['lane'])
                    if lane>=slots:raise ValueError('packed probe lane outside carrier')
                    logical=list(index);logical[axis]=index[axis]*slots+lane
                    if logical[axis]>=shape[axis]:raise ValueError('packed probe selects padding, not a legal logical element')
                linear=0
                for value,extent in zip(index,packed):linear=linear*extent+value
                probes[pid]={'logical_index':logical,'packed_index':index,'lane':lane,
                             'input':{'mode':'logical','index':input_index} if forward else {'mode':'packed','index':input_index,'lane':lane}}
                output={'carrier_index':linear,'carrier_byte_offset':linear*(carrier//8),'element_bit_offset':lane*element,
                        'logical_axis_index':logical[axis],'packed_axis_index':index[axis],'lane':lane}
                for key,value in output.items():put('p_'+pid+'__'+key,value)
                for axis_index,value in enumerate(logical):put('p_'+pid+'__logical_'+str(axis_index),value)
                for axis_index,value in enumerate(index):put('p_'+pid+'__packed_'+str(axis_index),value)
            details={'shape':shape,'packed_shape':packed,'axis':axis,'element_bits':element,'carrier_bits':carrier,
                     'scope':'fixed declared axis/slot mapping and finite probes; no proof that this mapping matches source semantics'}
        else:
            payload=number(row['payload_bytes']);beat=number(row['beat_bytes'],1);extent=_ceildiv(payload,beat)*beat
            values.update(payload_bytes=payload,full_beat_extent_bytes=extent,beats=_ceildiv(payload,beat),tail_padding_bytes=extent-payload,
                          last_valid_bytes=payload%beat or (beat if payload else 0))
            for query in rows(row.get('probes',[])):
                if not isinstance(query,dict) or set(query)!={'id','byte_offset','size_bytes'}:
                    raise ValueError('beat probe requires id, byte_offset and size_bytes')
                pid=query_id(query);offset=number(query['byte_offset']);size=number(query['size_bytes'],1)
                if offset+size>payload:raise ValueError('beat probe outside valid payload')
                first=offset//beat;last=(offset+size-1)//beat
                probes[pid]={'byte_offset':offset,'size_bytes':size}
                for key,value in {'aligned_byte_offset':first*beat,'byte_lane':offset%beat,'first_beat':first,'last_beat':last,'spanned_beats':last-first+1}.items():
                    put('p_'+pid+'__'+key,value)
            details={'beat_bytes':beat,'scope':'aligned-base offsets; full-beat reads require the returned extent, masked writes and actual allocation policy require separate interpretation'}
        if any(type(value) is not int or abs(value)>1e30 for value in values.values()):raise ValueError('excessive geometry result')
        return {'ok':True,'errors':[],'unknown':[],'values':values,'details':details,'probes':probes}
    except UnsupportedGeometry as error:
        return {'ok':False,'errors':[],'unknown':[str(error)],'values':{},'details':{},'probes':{}}
    except (KeyError,TypeError,ValueError,SyntaxError,ZeroDivisionError,OverflowError) as error:
        return {'ok':False,'errors':[str(error)],'unknown':[],'values':{},'details':{},'probes':{}}


def derive_all(rows,names,expression):
    """Publish only successfully derived, non-overwriting recipe variables."""
    result={'values':{},'details':[],'errors':[],'unknown':[]};ids=set();context=dict(names)
    if not isinstance(rows,list) or len(rows)>4096:
        result['errors'].append('abi_geometry must be a bounded array');return result
    for row in rows:
        try:
            rid=_id(row['id'])
            if rid in ids:raise ValueError('duplicate geometry id '+rid)
            ids.add(rid);derived=derive(row,context,expression)
            result['errors'].extend(rid+': '+error for error in derived['errors'])
            result['unknown'].extend(rid+': '+error for error in derived['unknown'])
            values={rid+'__'+key:value for key,value in derived['values'].items()}
            if set(values)&set(context):raise ValueError('geometry output would overwrite an existing value')
            if derived['ok']:
                context.update(values);result['values'].update(values)
                result['details'].append({'id':rid,'kind':row['kind'],'values':derived['values'],'details':derived['details'],'probes':derived['probes']})
        except (KeyError,TypeError,ValueError) as error:result['errors'].append(str(error))
    return result
