/* Business interaction extension. All writes use same-origin APIs. */
(function(){
  'use strict';
  const baseOpen=openCard, baseApply=applyOcrResult, baseTables=renderDetailTables, baseDetail=renderDetail;
  let draftId='', savedCard='', fieldsSource={}, restored=false, busy=false, context=null;
  const fieldMap={card_code:'合同编码',card_name:'卡片名称',card_fee:'费用类型',card_tenant:'承租方',
    card_lessor:'甲方',card_payer:'实际付款方',card_receiver:'实际收款方',card_addr:'租赁地址',
    card_prov:'省级',card_city:'市级',card_dist:'区镇',card_area:'片区',card_module:'业务模块',
    card_barea:'建筑面积',card_uarea:'实用面积',card_start:'租赁起始日',card_end:'租赁终止日',
    card_alg:'计费算法',card_settle:'结算方式',card_payday:'约定付款日期',card_dept:'提单部门',
    card_tax:'是否含税',card_ifrs:'是否IFRS',card_taxrate:'税率',card_rate:'IFRS折现率',card_prepaid:'预付租金',card_note:'备注'};
  const key=()=> 'lease-review-'+(ME&&ME.account||'anonymous');
  const clone=x=>JSON.parse(JSON.stringify(x));
  const msg=s=>{const e=document.getElementById('ocrStatus'); if(e)e.textContent=s;};
  function capture(){
    const values={}; Object.keys(fieldMap).forEach(k=>values[k]=gv(k));
    return {id:draftId,card_id:savedCard,values,fieldsSource,rows:window._ptable||[],sharing:window._sharing||[],
      oneoff:window._oneoff||[],segments:segs||[],text:window._ocrText||'',recognized:!!window._ocrApplied};
  }
  function backup(){
    if(!draftId||!document.getElementById('card_code'))return;
    try{sessionStorage.setItem(key(),JSON.stringify(capture()));}catch(e){msg('浏览器暂存空间不足，请保存到服务器后再离开。');}
  }
  function restore(d){
    draftId=d.id; savedCard=d.card_id||''; fieldsSource=d.fieldsSource||{};
    Object.keys(d.values||{}).forEach(k=>{let el=document.getElementById(k);if(el)el.value=d.values[k];});
    window._ptable=d.rows||[];window._sharing=d.sharing||[];window._oneoff=d.oneoff||[];
    window._ocrText=d.text||'';window._ocrApplied=d.recognized;segs=d.segments||[];
    window._ocrPeriods=[];restored=true; renderDetailTables();paintSegments();paintSources();
    msg('草稿已恢复，可以继续核对或重试保存。'); backup();
  }
  function paintSources(){
    Object.keys(fieldsSource).forEach(k=>{
      let el=document.getElementById(k);if(!el)return;
      let label=el.parentElement.querySelector('[data-source-label]');
      if(!label){label=document.createElement('small');label.dataset.sourceLabel='1';el.parentElement.appendChild(label);}
      const p=fieldsSource[k];label.textContent=p.source+(p.original!==undefined?' · 原值：'+p.original:'');
      label.className='hint';label.style.display='block';
    });
  }
  openCard=function(){
    baseOpen();draftId=crypto.randomUUID();savedCard='';fieldsSource={};restored=false;
    const receiver=document.getElementById('card_receiver');
    receiver.parentElement.querySelector('label').textContent='实际收款方';
    receiver.parentElement.insertAdjacentHTML('beforebegin',fld('出租方（甲方）','card_lessor',''));
    document.getElementById('card_name').parentElement.querySelector('label').textContent='场地简称（便于查找）';
    document.getElementById('card_rate').parentElement.querySelector('label').textContent='IFRS月折现率（0.003表示月利率0.3%）';
    document.getElementById('ocrSegHint').textContent='';
    document.getElementById('ocrDetail').before(document.getElementById('ocrSegHint'));
    const help=document.getElementById('ocrFile').closest('div').parentElement.querySelector('.hint');
    if(help)help.textContent='上传完整合同即可。当前 Demo 支持单文件不超过50MB、PDF最多50页；识别后仍需人工核对。正式版边界由服务器配置。';
    document.querySelector('[data-action="ocr-ai"]').style.display='none';
    document.getElementById('ocrDetail').insertAdjacentHTML('beforebegin',
      '<div class="hint">计费分段表示每月费用；付款计划表示约定支付，二者不能直接等同。推算值需人工核对。</div>'+
      '<div id="reviewSegments"></div><button type="button" class="btn btn-secondary btn-sm" id="newReviewSegment">添加计费分段</button>'+
      '<label style="display:block;margin:12px 0"><input type="checkbox" id="splitFees"> 本合同含租金和管理费，保存为两张关联费用卡</label>');
    document.getElementById('newReviewSegment').onclick=()=>{segs.push({s:gv('card_start'),e:gv('card_end'),months:'',amt:'',total:'',_source:'人工录入'});paintSegments();};
    document.getElementById('dlgCardBody').insertAdjacentHTML('afterbegin',
      '<div class="hint" style="margin-bottom:10px">导入合同 → 核对基础信息 → 核对费用与付款 → 保存草稿 → 复核提交</div>');
    const cached=sessionStorage.getItem(key());
    if(cached){try{
      const d=JSON.parse(cached),box=document.createElement('div');box.className='err-box';
      const btn=document.createElement('button');btn.className='btn btn-secondary';btn.textContent='恢复未完成草稿';
      btn.onclick=()=>{restore(d);box.remove();};box.textContent='当前账号有一份未完成的录入。 ';box.appendChild(btn);
      document.getElementById('dlgCardBody').prepend(box);
    }catch(e){}}
    paintSegments();
  };
  function paintSegments(){
    const el=document.getElementById('reviewSegments');if(!el)return;
    el.innerHTML='<h4>计费分段</h4>'+table(['起始日','终止日','月数','月金额','小计','来源'],segs.map((s,i)=>
      '<tr>'+['s','e','months','amt','total'].map(k=>'<td><input class="input" data-review-seg="'+i+'" data-key="'+k+'" value="'+esc(s[k]||'')+'"></td>').join('')+
      '<td>'+esc(s._source||'规则推算·待核对')+'</td></tr>').join(''));
  }
  applyOcrResult=function(data){
    const before={};Object.keys(fieldMap).forEach(k=>before[k]=gv(k));
    baseApply(data);
    const f=data.fields||{};
    const receiver=document.getElementById('card_receiver');
    if(receiver&&!before.card_receiver)receiver.value=f['实际收款方']||'';
    const lessor=document.getElementById('card_lessor');if(lessor&&!before.card_lessor)lessor.value=f['出租方']||'';
    Object.keys(fieldMap).forEach(k=>{
      if(gv(k)&&!before[k])fieldsSource[k]={source:'识别候选·待核对',original:gv(k)};
    });
    window._ptable.forEach((r,i)=>{r._source=(data.payment_table||[])[i]?'识别候选':'规则推算';r._original=clone(r);});
    window._sharing.forEach(r=>{r._source='识别候选';r._original=clone(r);});
    window._oneoff.forEach(r=>{r._source='识别候选';r._original=clone(r);});
    segs.forEach(r=>{r._source='规则推算·待核对';});
    document.getElementById('ocrSegHint').textContent='已生成 '+segs.length+' 个计费段，请在下方逐段核对，勿将付款合计直接当月租金。';
    if(window._ptable.some(r=>+r.mgmt>0)&&window._ptable.some(r=>+r.rent>0))document.getElementById('splitFees').checked=true;
    paintSources();paintSegments();renderDetailTables();backup();
    document.getElementById('card_code').scrollIntoView({block:'center'});
  };
  renderDetailTables=function(){
    baseTables();
    const root=document.getElementById('ocrDetail');if(!root)return;
    root.querySelectorAll('table').forEach((t,n)=>{
      const rows=[window._ptable,window._sharing,window._oneoff][n]||[];
      const head=document.createElement('th');head.textContent='来源 / 原值';t.querySelector('thead tr').appendChild(head);
      t.querySelectorAll('tbody tr').forEach((tr,i)=>{
        const r=rows[i]||{},td=document.createElement('td');td.dataset.rowSource='1';
        td.textContent=r._source||'人工录入';
        if(r._original){const d=document.createElement('details'),sum=document.createElement('summary');sum.textContent='原始值';d.appendChild(sum);
          const txt=document.createElement('div');txt.textContent=Object.entries(r._original).filter(([k])=>!k.startsWith('_')).map(([k,v])=>k+'：'+v).join('；');d.appendChild(txt);td.appendChild(d);}
        tr.appendChild(td);
      });
    });
  };
  document.addEventListener('input',e=>{
    if(fieldMap[e.target.id]){
      const k=e.target.id; fieldsSource[k]=Object.assign({},fieldsSource[k]||{original:''},{source:'人工修改'});
      paintSources(); backup();
    }
    if(e.target.dataset.reviewSeg!==undefined){
      const s=segs[+e.target.dataset.reviewSeg];s[e.target.dataset.key]=e.target.value;s._source='人工修改';
      backup();
    }
    if(e.target.dataset.tbl){
      const a={pt:window._ptable,sh:window._sharing,oo:window._oneoff}[e.target.dataset.tbl];
      if(a&&a[+e.target.dataset.i]){a[+e.target.dataset.i]._source='人工修改';
        const cell=e.target.closest('tr').querySelector('[data-row-source]');if(cell)cell.firstChild.textContent='人工修改';}
    }
  });
  document.addEventListener('change',()=>{if(document.getElementById('dlgCard').classList.contains('show')){backup();paintSegments();}});
  createCard=async function(){
    if(busy)return;
    const b={};Object.entries(fieldMap).forEach(([id,k])=>b[k]=gv(id));
    b['OCR状态']=window._ocrApplied?'已识别待核对':'未识别';
    if(!b['合同编码']){msg('请填写合同编号后保存草稿。');document.getElementById('card_code').focus();return;}
    const all=capture(),split=document.getElementById('splitFees').checked;
    if(split && !window._ptable.length){msg('拆分费用卡需要先录入每期租金和管理费。');return;}
    if(split && savedCard){msg('当前为已保存卡片的恢复编辑；请分别修改两张费用卡，不重复拆分。');return;}
    let sharing=window._sharing.map(s=>({'成本中心':s.cost_center,'分摊比例':s.ratio,'生效日':b['租赁起始日'],'倒挤':false,'变更依据':s.note||s.dept||''}));
    busy=true;document.getElementById('dlgCardOk').disabled=true;backup();
    try{
      const fees=split?['租金','管理费']:[b['费用类型']];
      const ids=[];
      for(const fee of fees){
        const card=Object.assign({},b,{'费用类型':fee});
        if(fee!=='租金')card['是否IFRS']='否';
        let feeSegments=segs;
        if(split){
          const rows=window._ptable.map(r=>Object.assign({},r,{total:fee==='租金'?r.rent:r.mgmt}));
          const periods=_paymentTableToPeriods(rows,b['租赁起始日']);
          feeSegments=periodsToSegs(b['租赁起始日'],b['租赁终止日'],periods);
          if(!feeSegments.length)throw Error('付款期间无法转换为计费段，请补全包含年份的期间后重试。');
        }
        msg('正在保存'+fee+'草稿与来源记录…');
        const payload={request_id:draftId+(split?'-'+fee:''),card_id:split?'':savedCard,card,
          rows:window._ptable,segments:feeSegments,sharing,oneoff:fee==='租金'?window._oneoff:[],
          provenance:{fields:fieldsSource,original_text:window._ocrText||''}};
        const r=await API.req('POST','/api/intake',payload);
        if(r.data.card_id&&!split)savedCard=r.data.card_id;
        backup();
        if(!r.data.ok)throw Error((r.data.completed||[]).join('、')+'已保存；'+(r.data.error||'保存未完成'));
        ids.push(r.data.card_id);
      }
      sessionStorage.removeItem(key());draftId='';closeDlg('dlgCard');
      await showDetail(ids[0]);if(ids.length>1)alert('已保存租金和管理费两张卡；请分别复核并提交。');
    }catch(e){msg(e.message+'。草稿已保留，修正后再次点击保存可继续，不会重复建卡。');}
    finally{busy=false;document.getElementById('dlgCardOk').disabled=false;}
  };
  renderDetail=async function(id){
    await baseDetail(id);
    id=id||(curCard&&curCard.record_id);if(!id)return;
    const r=await API.req('GET','/api/cards/'+id+'/context');if(!r.data.ok)return;
    context=r.data;const d=context.intake;
    const box=document.createElement('section');box.className='card';box.style.marginBottom='16px';
    const history=context.approval_events||[];
    box.innerHTML='<div class="card-b"><h3>复核与审批摘要</h3><p>'+
      esc(fv(curCard['合同编码'])+'｜'+fv(curCard['卡片名称'])+'｜'+fv(curCard['费用类型']))+
      '</p><p>计费段 '+context.overview.length+' 条；合同法台账 '+context.ledger.length+' 行；'+
      '校验问题 '+context.validation.errors.length+' 项。</p>'+
      (d&&!d.complete?'<div class="err-box">保存未完成，请恢复草稿；暂不能提交。</div>':'')+
      (d&&d.actor===(ME&&ME.account)&&['草稿','退回'].includes(fv(curCard['审批状态']))?
      '<button class="btn btn-primary" id="resumeIntake">继续核对完整草稿</button>':'')+
      (d&&d.complete&&d.actor===(ME&&ME.account)&&['草稿','退回'].includes(fv(curCard['审批状态']))&&context.ledger.length&&!context.validation.errors.length?
      ' <button class="btn btn-primary" data-action="card-submit" data-id="'+esc(id)+'">核对完成，提交审批</button>':'')+
      (fv(curCard['审批状态'])==='待审'&&history.length&&history[history.length-1].actor===(ME&&ME.account)?
        '<button class="btn btn-secondary" data-action="withdraw" data-id="'+esc(id)+'">撤回本次提交</button>':'')+
      '<h4>审批时间线</h4>'+history.map(e=>'<p>'+esc((e.time||'').replace('T',' ').slice(0,19)+' · '+(e.name||e.actor||'系统')+' · '+e.action+(e.opinion?'：'+e.opinion:''))+'</p>').join('')+
      '<details><summary>识别与人工修改依据</summary><div class="source-review">'+
      (d?sourceReadable(d.payload):'<p class="hint">旧卡片尚无来源快照</p>')+'</div></details></div>';
    document.getElementById('content').insertBefore(box,document.querySelector('#content .tabs'));
    if(d&&fv(curCard['审批状态'])==='待审'&&canApprove()&&d.actor!==(ME&&ME.account)){
      box.querySelector('.card-b').insertAdjacentHTML('afterbegin','<button class="btn btn-primary" data-action="approve" data-id="'+esc(id)+'">通过</button> <button class="btn btn-secondary" data-action="reject" data-id="'+esc(id)+'">退回</button>');
    }
    const btn=document.getElementById('resumeIntake');
    if(btn)btn.onclick=()=>{
      openCard();const p=d.payload,values={};Object.entries(fieldMap).forEach(([key,f])=>values[key]=p.card[f]||'');
      restore({id:d.request_id,card_id:id,values,fieldsSource:p.provenance&&p.provenance.fields,
        rows:p.rows,segments:(p.segments||[]).map(s=>({s:s.s||s['租赁起始日'],e:s.e||s['租赁终止日'],months:s.months||s['月份数'],amt:s.amt||s['月金额'],total:s.total||s['金额小计'],_source:s._source||'已保存草稿'})),
        sharing:(p.sharing||[]).map(s=>({cost_center:s['成本中心'],ratio:s['分摊比例'],note:s['变更依据']})),
        oneoff:p.oneoff,text:p.provenance&&p.provenance.original_text,recognized:p.card['OCR状态']!=='未识别'});
    };
  };
  function sourceReadable(payload){
    const p=payload||{}, fields=p.provenance&&p.provenance.fields||{};
    const labels={contract_code:'合同编号',card_name:'卡片名称',tenant:'承租主体',payer:'付款主体',lessor:'出租主体',receiver:'收款主体',address:'租赁地址',area:'租赁面积',start:'租赁起始日',end:'租赁终止日',rate:'月利率'};
    const fs=Object.keys(labels).filter(k=>fields[k]||p.card&&p.card[k]).map(k=>{const x=fields[k]||{};return '<tr><th>'+labels[k]+'</th><td>'+esc(x.value||p.card[k]||'')+'</td><td><span class="source-badge">'+esc(x.source||'人工录入')+'</span>'+(x.original!==undefined&&x.original!==x.value?'<details><summary>查看原值</summary>'+esc(String(x.original))+'</details>':'')+'</td></tr>';}).join('');
    const rows=(p.rows||[]).map(r=>'<tr><td>'+esc(r.s||r['租赁起始日']||'')+'</td><td>'+esc(r.e||r['租赁终止日']||'')+'</td><td>'+esc(r.total||r['金额小计']||'')+'</td><td>'+esc(r._source||'人工录入')+'</td></tr>').join('');
    return '<table class="mini-table"><thead><tr><th>字段</th><th>当前值</th><th>来源</th></tr></thead><tbody>'+fs+'</tbody></table>'+(rows?'<h5>金额明细</h5><table class="mini-table"><thead><tr><th>起始</th><th>终止</th><th>金额</th><th>来源</th></tr></thead><tbody>'+rows+'</tbody></table>':'');
  }
  renderPayments=function(list){
    const rows=list.map(p=>{
      const paid=[true,'是',1].includes(fv(p['已付'])),locked=paid||['待审','通过'].includes(p._approval);
      return '<tr><td><b>'+esc(fv(p['合同编码'])||'缺合同编号')+'</b><br>'+esc(fv(p['卡片名称'])||fv(p['租赁地址'])||'')+'</td>'+
        '<td>'+esc(fv(p['费用类型']))+'</td><td>'+esc(fv(p['费用所属期间']))+'</td><td>'+money(p['计划付款金额'])+'</td>'+
        '<td>'+esc(fv(p['约定付款日期']))+'</td><td>'+esc(p._approval||'未提交')+'</td><td>'+(paid?'已付':'未付')+'</td>'+
        '<td>'+(!locked?'<button class="btn btn-secondary btn-sm" data-action="pay-toggle" data-id="'+esc(p.record_id)+'">'+(paySel.has(p.record_id)?'已勾选':'勾选')+'</button>':'')+
        (!paid&&p._approval==='通过'&&canCreate()?'<button class="btn btn-secondary btn-sm" data-pay-result="'+esc(p.record_id)+'">登记实际支付</button>':'')+'</td></tr>';
    }).join('');
    renderList('付款管理','审批通过不代表实际支付',
      '<button class="btn btn-secondary" data-action="pay-gen">生成已批准合同的付款计划</button> '+
      '<button class="btn btn-primary" data-action="pay-submit">提交已选 '+paySel.size+' 笔</button>'+
      table(['合同 / 场地','费用','所属期间','计划金额','付款日','审批','支付','操作'],rows)+'<div id="paymentBatches"></div>');
    API.req('GET','/api/payment-batches').then(r=>{
      const el=document.getElementById('paymentBatches');if(!el||!r.data.ok)return;
      el.innerHTML='<h3>付款审批批次</h3>'+r.data.batches.map(b=>'<p>'+esc(b.batch+' · '+b.rows.length+'笔 · '+b.state+' · '+(b.opinion||''))+
        (['付款审批人','管理员'].includes(ME.role)&&b.state==='待审'&&b.actor!==ME.account?
          ' <button class="btn btn-secondary" data-pay-decide="通过" data-batch="'+esc(b.batch)+'">通过</button>'+
          ' <button class="btn btn-secondary" data-pay-decide="退回" data-batch="'+esc(b.batch)+'">退回</button>':'')+'</p>').join('');
    });
  };
  paySubmit=async function(){
    if(!paySel.size)return alert('请先勾选付款明细');
    const r=await API.req('POST','/api/payments/submit',{record_ids:Array.from(paySel)});
    if(!r.data.ok)return alert(r.data.error);await showPayments();
  };
  document.addEventListener('click',async e=>{
    const dec=e.target.closest('[data-pay-decide]'),pay=e.target.closest('[data-pay-result]');
    if(dec){
      const opinion=prompt('填写审批意见（退回必填）');if(opinion===null)return;
      const r=await API.req('POST','/api/payment-batch/decide',{batch:dec.dataset.batch,action:dec.dataset.payDecide,opinion});
      if(!r.data.ok)alert(r.data.error);else showPayments();
    }
    if(pay){
      const amount=prompt('实际支付金额（根据真实支付结果填写）');if(amount===null)return;
      const date=prompt('实际支付日期 YYYY-MM-DD');if(date===null)return;
      const r=await API.req('POST','/api/payments/record/'+pay.dataset.payResult,{'实付金额':amount,'实付日期':date});
      if(!r.data.ok)alert(r.data.error);else showPayments();
    }
  });
})();
