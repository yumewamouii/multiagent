let chart;
async function loadDashboard(){
  const product=document.getElementById('product').value;
  const sourceIdRaw=document.getElementById('source').value;
  const payload={product_name:product,page:1,page_size:20};
  if(sourceIdRaw) payload.source_id=Number(sourceIdRaw);
  const res=await fetch('/insights/dashboard',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
  const data=await res.json();
  document.getElementById('review_count').textContent=data.kpi?.review_count ?? '-';
  document.getElementById('avg_rating').textContent=data.kpi?.avg_rating ?? '-';
  document.getElementById('negative_ratio').textContent=((data.kpi?.negative_ratio ?? 0)*100).toFixed(1)+'%';
  const tbody=document.getElementById('rows'); tbody.innerHTML='';
  (data.items||[]).forEach(i=>{const tr=document.createElement('tr'); tr.innerHTML=`<td>${i.run_id}</td><td>${i.product_name}</td><td>${i.confidence}</td><td>${i.created_at}</td><td>${i.summary||''}</td>`; tbody.appendChild(tr);});
  const pos=((data.kpi?.positive_ratio ?? 0)*100).toFixed(1); const neg=((data.kpi?.negative_ratio ?? 0)*100).toFixed(1);
  if(chart) chart.destroy();
  chart=new Chart(document.getElementById('sentimentChart'),{type:'doughnut',data:{labels:['Positive','Negative'],datasets:[{data:[pos,neg],backgroundColor:['#31c48d','#ff6b6b']}]}})
}
document.getElementById('load').onclick=loadDashboard;
document.getElementById('export').onclick=async()=>{const product=document.getElementById('product').value;const r=await fetch('/insights/dashboard/export',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({product_name:product})});const t=await r.text();const blob=new Blob([t],{type:'text/csv'});const a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download='dashboard.csv';a.click();};
loadDashboard();
