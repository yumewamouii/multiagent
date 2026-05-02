const out=document.getElementById('out');
async function syncQuery(){
 const query=document.getElementById('q').value; const top_k=Number(document.getElementById('topk').value||5);
 const res=await fetch('/multiagent/query',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({query,top_k})});
 out.textContent=JSON.stringify(await res.json(),null,2);
}
async function asyncQuery(){
 const query=document.getElementById('q').value; const top_k=Number(document.getElementById('topk').value||5);
 const res=await fetch('/multiagent/query/async',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({query,top_k})});
 const job=await res.json(); out.textContent='Job created: '+job.job_id+'\nPolling...';
 let done=false; while(!done){await new Promise(r=>setTimeout(r,1500)); const st=await fetch('/multiagent/jobs/'+job.job_id); const data=await st.json(); out.textContent=JSON.stringify(data,null,2); done=(data.status==='done'||data.status==='failed');}
}
document.getElementById('sync').onclick=syncQuery; document.getElementById('async').onclick=asyncQuery;
