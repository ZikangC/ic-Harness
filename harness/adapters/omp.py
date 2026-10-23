"""Configured OMP transport, parsing and content-preserving JSON repair."""
import hashlib
import os
import json
import re
import subprocess
import time
from harness.runtime.run_store import dump, digest

from harness.adapters.json_repair import parse_json, json_decoder, serialization_frame, serialization_tokens


class OMPTransportError(RuntimeError):
    """A terminal provider completion failed; its text is not a model answer."""


def final_text(events, blocked_ids=()):
    """Select a completed answer; failed unavailable-tool attempts are not answers."""
    answers=[];ended=False;last_reason=None;native=set();failed=set(blocked_ids)
    for line in events.splitlines():
        event=json.loads(line)
        if event.get('type')=='agent_end':ended=True
        if event.get('type')=='tool_execution_end' and event.get('isError') is True:
            failed.add(event.get('toolCallId'))
        if event.get('type')!='message_end':continue
        message=event.get('message',{})
        if message.get('role')=='toolResult' and message.get('isError') is True:
            failed.add(message.get('toolCallId'))
        if message.get('role')!='assistant':continue
        last_reason=message.get('stopReason');content=message.get('content',[])
        if last_reason=='toolUse':
            native.update(c.get('id') for c in content if c.get('type')=='toolCall')
            continue
        if last_reason!='stop':continue
        if any(x.get('type') not in ('text','thinking') for x in content):raise ValueError('unexpected native tool/nontext final response')
        answer=''.join(x['text'] for x in content if x.get('type')=='text')
        if answer.strip():answers.append(answer)
    if last_reason in ('error','aborted','length'):raise OMPTransportError('OMP terminal assistant failure; reason='+last_reason+'; no provider diagnostics stored')
    if native-failed:raise ValueError('native tool attempt lacks failed/unavailable result evidence')
    if last_reason!='stop':raise ValueError('OMP assistant completion: '+str(last_reason))
    if not ended or len(answers)!=1:raise ValueError('expected one completed OMP assistant answer')
    return answers[0]



def observed_turns(events):
    """Count observable completed assistant events, including failing CLI output."""
    if isinstance(events,bytes):events=events.decode('utf-8',errors='replace')
    count=0
    for line in (events or '').splitlines():
        try:
            event=json.loads(line)
            if event.get('type')=='message_end' and event.get('message',{}).get('role')=='assistant':count+=1
        except (ValueError,TypeError):continue
    return count


def call_model(out, label, role, prompt, config, root, timeout, thinking='off', parser=None, before_call=None, record_observed=None):
    deadline = time.monotonic() + timeout
    if before_call:
        before_call()
    generation = 1
    while (out / (label + f'.call-{generation}.call.json')).exists():
        generation += 1
    label = label + f'.call-{generation}'
    prompt_path = out / (label + '.prompt.txt')
    prompt_path.write_text(prompt, encoding='utf-8')
    # OMP's native stdin path forwards text unchanged. Its @file path adds a
    # wrapper and silently skips oversized text attachments, so use stdin for
    # the exact message previously carried in the positional argv placeholder.
    message = 'Task instructions:\n' + prompt
    message_bytes = message.encode('utf-8')
    model = config['roles'][role]
    template = config['runtime']['command_template']
    if template.count('{prompt}') != 1 or any('{prompt}' in part and part != '{prompt}' for part in template):
        raise ValueError('OMP native stdin delivery requires exactly one standalone {prompt} message placeholder')
    argv = [part.format(project_root=str(root), model=model)
            for part in template if part != '{prompt}']
    # Existing OMP adapter/template; explicit input-only calls exclude unrelated project rules/tools.
    guard=out/'snapshot/harness/adapters/omp_isolation.js'
    if not guard.is_file():raise RuntimeError('frozen OMP tool-isolation extension missing')
    isolation_path=out/(label+'.isolation.json')
    process_env=dict(os.environ, HARNESS_TOOL_ISOLATION_RECEIPT=str(isolation_path.resolve()))
    flags = ['--mode','json','--extension',str(guard.resolve()),'--no-tools', '--no-extensions', '--no-skills', '--no-rules', '--no-session',
             '--no-title', '--no-lsp', '--thinking', thinking, '--max-time', str(timeout - 5), '--system-prompt',
             'Analyze only supplied inputs. Your entire answer must be the requested data or document, without public reasoning, planning, narration or a second draft. Serialize JSON compactly, without indentation or formatting-only line breaks, preserving all required fields, values and meaning. Keep necessary explanations concise. Keep Markdown readable with normal paragraphs, lists and line breaks. Follow the current-step response format exactly. Only claim tool execution when actual results are supplied by the caller. Tool requests are plain text protocol messages, never native tool-call markup.']
    argv[1:1] = flags
    metadata = {'role': role, 'model': model, 'prompt_sha256': digest(prompt.encode('utf-8')), 'timeout_seconds': timeout, 'thinking': thinking, 'transport':'omp-json-events; text blocks only',
                'delivery':{'kind':'native_stdin','prefix':'Task instructions:\n','message_sha256':digest(message_bytes),
                            'message_bytes':len(message_bytes),'content':'exact prior positional message; unchanged prompt; no @file wrapper'}}
    print('model request: ' + label + ' (' + model + ')', flush=True)
    dump(out / (label + '.call.json'), metadata)
    try:
        p = subprocess.run(argv, input=message, cwd=root, capture_output=True, text=True, encoding='utf-8', timeout=timeout, check=False, env=process_env)
        metadata['exit_code'] = p.returncode
        # Do not save CLI stderr or profile/env: may include provider/auth diagnostics.
        metadata['stderr_present'] = bool(p.stderr)
        dump(out / (label + '.call.json'), metadata)
        if p.returncode:
            observed=observed_turns(p.stdout)
            metadata['observed_assistant_turns']=observed
            dump(out / (label + '.call.json'),metadata)
            if record_observed:record_observed(max(0,observed-1))
            classification = 'time_limit' if any(word in p.stderr.lower() for word in ('time limit', 'timed out', 'max-time', 'timeout')) else 'cli_or_provider_failure'
            metadata['error_class'] = classification
            dump(out / (label + '.call.json'), metadata)
            raise RuntimeError('OMP failed; exit=' + str(p.returncode) + '; class=' + classification + '; no credential diagnostics stored')
        summaries=[];last_text=''
        for line in p.stdout.splitlines():
            try:
                event=json.loads(line);message=event.get('message',{})
                if event.get('type')=='message_end' and message.get('role')=='assistant':
                    content=message.get('content',[])
                    summaries.append({'stop_reason':message.get('stopReason'),'content_types':[c.get('type') for c in content], 'native_tool_names':[c.get('name') for c in content if c.get('type')=='toolCall'], 'error_class':next((label for label in ('timeout','aborted','context','length','429','403','500','502','503','signature','tool') if label in str(message.get('errorMessage','')).lower()),'unspecified') if message.get('stopReason')=='error' else message.get('stopReason') if message.get('stopReason') in ('aborted','length') else None})
                    last_text=''.join(c['text'] for c in content if c.get('type')=='text')
            except (ValueError,KeyError,TypeError):pass
        dump(out/(label+'.transport.json'),{'assistant_messages':summaries,'scope':'no thinking fields or raw event stream retained'})
        if last_text:(out/(label+'.response.txt')).write_text(last_text)
        # Charge observed extra OMP turns as well as the initially reserved call.
        # OMP can internally recover from unavailable native-tool hallucinations.
        extra_turns=max(0,len(summaries)-1)
        if record_observed:record_observed(extra_turns)
        else:
            for _ in summaries[1:]:
                if before_call:before_call()
        if summaries and summaries[-1]['stop_reason'] in ('error','aborted','length'):
            classification=summaries[-1]['error_class']
            metadata.update(observed_assistant_turns=len(summaries),error_class=classification)
            dump(out/(label+'.call.json'),metadata)
            raise OMPTransportError('OMP terminal assistant failure; class='+classification+'; no provider diagnostics stored')
        isolation=json.loads(isolation_path.read_text()) if isolation_path.is_file() else {}
        if not isolation.get('checks') or any(c.get('active_tool_names')!=[] for c in isolation['checks']):raise RuntimeError('OMP native tool isolation not confirmed')
        answer=final_text(p.stdout,blocked_ids=[x['id'] for x in isolation.get('blocked',[])])
        (out / (label + '.response.txt')).write_text(answer)
        try:
            return (parser or parse_json)(answer)
        except (ValueError, json.JSONDecodeError) as parse_error:
            if role == 'evidence' or not isinstance(parse_error, json.JSONDecodeError):
                raise
            # Repair syntax only. A valid JSON object with invalid protocol fields
            # must return to its author; it is not a serialization repair request.
            try:
                prefix, payload, suffix = serialization_frame(answer)
                original_tokens = serialization_tokens(payload)
            except ValueError as guard_error:
                dump(out / (label + '.normalization.json'), {
                    'source_sha256': digest(answer.encode()), 'accepted': False,
                    'values_order_nesting_preserved': False, 'repair_called': False,
                    'error_class': 'ineligible_payload', 'reason': str(guard_error),
                    'scope': 'no repair of truncated, ambiguous or non-JSON content'})
                raise parse_error
            repair_prompt = 'Repair JSON serialization ONLY. Preserve every field, value, order and nesting. Fix missing commas/colons or necessary string escaping; hexadecimal numeric literals may be converted exactly to decimal. Do not fill truncated content, add/remove fields, change expressions, or solve the design. Return the same one complete JSON object only.\nPARSER ERROR: ' + str(parse_error) + '\nRAW JSON PAYLOAD:\n' + payload
            receipt = {'source_sha256': digest(answer.encode()), 'accepted': False,
                       'values_order_nesting_preserved': False,
                       'repair_called': True, 'error_class': 'repair_pending',
                       'scope': 'serialization only; original response retained; no design acceptance'}
            dump(out / (label + '.normalization.json'), receipt)
            try:
                remaining = int(deadline - time.monotonic())
                if remaining <= 5:
                    raise TimeoutError('serialization repair deadline exhausted; original response remains unaccepted')
                repaired = call_model(out, label + '-serialization', 'evidence', repair_prompt, config, root, remaining, 'off', before_call=before_call, record_observed=record_observed)
            except Exception as repair_error:
                receipt.update(error_class='repair_failed', error_type=type(repair_error).__name__)
                dump(out / (label + '.normalization.json'), receipt)
                raise
            normalized = json.dumps(repaired, ensure_ascii=False, allow_nan=False)
            framed = prefix + normalized + suffix
            (out / (label + '.normalized.response.txt')).write_text(framed)
            same = original_tokens == serialization_tokens(normalized)
            receipt.update(normalized_sha256=digest(framed.encode()),
                           values_order_nesting_preserved=same, error_class=None)
            if not same:
                receipt['error_class'] = 'content_changed'
                dump(out / (label + '.normalization.json'), receipt)
                raise ValueError('serialization repair changed content/order/nesting; rejected')
            try:
                result = (parser or parse_json)(framed)
            except ValueError:
                receipt['error_class'] = 'protocol_rejected'
                dump(out / (label + '.normalization.json'), receipt)
                raise
            receipt['accepted'] = True
            dump(out / (label + '.normalization.json'), receipt)
            return result
    except subprocess.TimeoutExpired as expired:
        observed=observed_turns(expired.stdout)
        metadata["observed_assistant_turns"]=observed
        metadata['error'] = 'timeout'
        dump(out / (label + '.call.json'), metadata)
        if record_observed:record_observed(max(0,observed-1))
        raise RuntimeError('OMP request timed out; resume retries without accepting partial artifacts')


