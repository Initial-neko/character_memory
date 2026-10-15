"""Explicit paid, isolated Person -> fresh Claude -> Person acceptance.

Reads operator model routing from --config, but never opens its formal DB.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import tempfile
import uuid

from character_memory.app import build_model
from character_memory.config import load_settings
from character_memory.domain.models import Event, EventType
from character_memory.memory.embedding import DeterministicEmbedding
from character_memory.memory.recall import VectorRecall
from character_memory.runtime.claude_process import ClaudePrintRunner
from character_memory.runtime.direct_research import DirectResearchService
from character_memory.runtime.person_runtime import PersonRuntime
from character_memory.storage.sqlite import SQLiteStore


class NoPublicFetch:
    def observe(self, *args, **kwargs):
        raise RuntimeError('This arithmetic acceptance does not fetch external sources')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',default='config.yaml')
    parser.add_argument('--output',required=True,help='Local JSON evidence; may contain test-only model output')
    args=parser.parse_args()
    config=Path(args.config).resolve()
    config_hash=hashlib.sha256(config.read_bytes()).hexdigest()
    original=load_settings(str(config))
    output=Path(args.output).resolve()
    output.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='cm-direct-claude-') as temporary:
        root=Path(temporary).resolve()
        folder=root/'personas'/'echo'
        folder.mkdir(parents=True)
        (folder/'persona.yaml').write_text('id: echo\nname: Echo\n',encoding='utf-8')
        (folder/'capabilities.yaml').write_text('capabilities: [claude_task]\nskills: []\n',encoding='utf-8')
        settings=original.model_copy(update={'db_path':str(root/'acceptance.db'),'embedding_provider':'deterministic',
            'direct_claude_tasks_enabled':True,'claude_execution_daily_max':2,
            'claude_execution_max_budget_usd':0.02,'claude_execution_timeout_seconds':60,
            'claude_execution_max_turns':2,'claude_execution_max_tool_calls':1})
        assert Path(settings.db_path).resolve().is_relative_to(root)
        store=SQLiteStore(settings.db_path)
        model=build_model(settings)
        observer=NoPublicFetch()
        runner=ClaudePrintRunner(observer,root/'claude',timeout_seconds=60,max_turns=2,max_tool_calls=1,
                                 model=settings.claude_execution_model,max_budget_usd=.02)
        emb=DeterministicEmbedding()
        runtime=PersonRuntime(store,VectorRecall(store,emb),emb,model,
            '你叫Echo，是自知为AI的测试助手，表达自然、清楚、乐于按照用户要求验证系统。'
            '用户明确要求委托时，在允许范围内使用DELEGATE_TASK；如实报告执行失败，不假装调用成功。')
        runtime.research_service=DirectResearchService(settings,store,observer,runner,
            lambda:[{'id':'echo','persona_path':str(folder/'persona.yaml')}])
        results=[]
        marker='FIRST_'+uuid.uuid4().hex[:12]
        try:
            for text in [f'为验收委托功能，请把“计算17加28，只返回数字45，并带上当前标签{marker}”交给Claude执行器，收到实际结果后再告诉我。',
                         '请另开一次独立委托：计算31加42，只返回数字73。执行器是否知道前一次的标签？不知道就明确不知道，不要把旧标签发送给它。']:
                before=store.conn.execute('SELECT count(*) FROM capability_executions').fetchone()[0]
                event=Event(character_id='echo',event_type=EventType.USER_MESSAGE,event_time=datetime.now(timezone.utc),
                    content=text,metadata={'channel':'DIRECT','conversation_id':'echo:acceptance'})
                result=runtime.handle(event)
                rows=store.conn.execute('SELECT request_json,status,result_json FROM capability_executions ORDER BY rowid').fetchall()
                row=rows[-1] if len(rows)>before else None
                receipt=json.loads(row['result_json']) if row and row['result_json'] else {}
                request=json.loads(row['request_json']) if row else {}
                results.append({'source_event_id':result.event.id,'delegated':len(rows)>before,
                    'status':row['status'] if row else 'NO_DELEGATION', 'sent_task':request.get('arguments',{}).get('query'),
                    'reason':receipt.get('reason'), 'execution':receipt.get('data',{}).get('execution',{}),
                    'executor_answer':receipt.get('data',{}).get('assistant_response',''),
                    'knowledge_scope':receipt.get('data',{}).get('knowledge_scope'),
                    'person_actions':[a.model_dump(mode='json') for a in result.reaction.actions]})
            first,second=results
            passed=all(r['delegated'] and r['status']=='SUCCESS' for r in results)
            passed=passed and bool(first['execution'].get('session_id')) and first['execution'].get('session_id')!=second['execution'].get('session_id')
            passed=passed and marker not in (second['sent_task'] or '') and marker not in second['executor_answer']
            report={'passed':passed,'scope':'real Person + real CLI; arithmetic/model response, no public-web quality claim',
                    'formal_database_opened':False,'formal_config_unchanged':config_hash==hashlib.sha256(config.read_bytes()).hexdigest(),
                    'results':results}
            output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
            print(json.dumps({'passed':passed,'statuses':[r['status'] for r in results],'report':str(output)}))
            return 0 if passed else 1
        finally:
            runner.close()
            model.close()
            store.close()


if __name__=='__main__':
    raise SystemExit(main())
