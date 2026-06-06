/*
 * policylogic-share.js
 * Drop-in scorecard → Instagram carousel generator for PolicyLogic.io
 * Static site, client-side only. No backend.
 *
 * USAGE (per scorecard page):
 *   <script src="/policylogic-share.js"></script>
 *   <button id="share-btn">Share scorecard</button>
 *   <script>
 *     PolicyLogicShare.attach('#share-btn', {
 *       name: 'Sen. Jane Doe',
 *       sub: 'Democrat · Ohio · 118th Congress',
 *       since: '2017',
 *       total: 24,                         // total promises tracked
 *       s1: ['Committee', 'Armed Services'],
 *       s2: ['Next election', '2028'],
 *       web: 'doe.senate.gov',
 *       rule: 'ranked by stakes',          // selection-rule label
 *       promises: [                         // pre-sorted; module takes top 12
 *         { type:'Quantitative', text:'…', delivery:'D3', diff:'H2', impact:'High', score:18, detail:'…' },
 *         …
 *       ],
 *       sources: ['…', '…']
 *     });
 *   </script>
 *
 * The module reads data from the config object you pass — it does not scrape the DOM,
 * so there is one source of truth. If your page already holds the scorecard as a JS
 * object, pass it straight in (mapping field names as needed).
 */
(function (global) {
  'use strict';

  var C = { ink:'#1a1714', paper:'#f5f0e8', cream:'#ede8dc', rule:'#d4c9b4',
            gold:'#b8892a', goldL:'#d4a843', gray:'#6b6459', slate:'#3a4654' };
  var TYPE_COLOR  = { Quantitative:C.gold, Qualitative:C.green || '#1a6b3c', Negative:C.slate };
  var TYPE_GLOSS  = { Quantitative:'a measurable target', Qualitative:'an action or outcome', Negative:'a pledge not to act' };
  var DELIVERY    = { D4:'Delivered', D3:'Substantially delivered', D2:'Partial', D1:'Minimal', D0:'Not delivered' };
  var DIFF        = { H3:'Structural', H2:'Legislative', H1:'Executive' };
  var W = 1080, H = 1080, PAD = 96, MAX_PROMISES = 12;
  var FONTS = ['900 80px "Playfair Display"','700 60px "Playfair Display"','italic 700 40px "Playfair Display"',
               '700 40px "DM Sans"','300 36px "DM Sans"','italic 300 26px "DM Sans"','500 22px "DM Mono"'];

  // ---- text helpers ----
  function wrap(ctx, t, maxW) {
    var words = String(t).split(' '), lines = [], line = '';
    for (var i=0;i<words.length;i++){ var test=line?line+' '+words[i]:words[i];
      if (ctx.measureText(test).width>maxW && line){ lines.push(line); line=words[i]; } else line=test; }
    if (line) lines.push(line); return lines;
  }
  function sp(ctx, t, ls, x, y, al) {
    var ch=String(t).split(''), tot=0, i;
    for (i=0;i<ch.length;i++) tot += ctx.measureText(ch[i]).width + ls;
    tot -= ls;
    var cx = al==='center'? x-tot/2 : al==='right'? x-tot : x;
    for (i=0;i<ch.length;i++){ ctx.fillText(ch[i],cx,y); cx += ctx.measureText(ch[i]).width + ls; }
    return tot;
  }
  function fit(ctx, t, maxW, weight, fam, start, min) {
    var s=start; while (s>min){ ctx.font=weight+' '+s+'px "'+fam+'"'; if (ctx.measureText(t).width<=maxW) break; s-=2; } return s;
  }
  function newCanvas(bg) {
    var cv=document.createElement('canvas'); cv.width=W; cv.height=H;
    var ctx=cv.getContext('2d'); ctx.fillStyle=bg; ctx.fillRect(0,0,W,H); ctx.textBaseline='alphabetic';
    return { cv:cv, ctx:ctx };
  }
  function topbar(ctx, col){ ctx.fillStyle=col||C.gold; ctx.fillRect(0,0,W,12); }

  // ---- slide renderers (ported verbatim from the validated v3 design) ----
  function coverSlide(d){ var o=newCanvas(C.ink), x=o.ctx; topbar(x);
    x.fillStyle=C.goldL; x.font='500 22px "DM Mono"'; sp(x,'POLICYLOGIC · EST. 2026 · NONPARTISAN',5,W/2,110,'center');
    x.strokeStyle='rgba(245,240,232,0.25)'; x.lineWidth=1; x.beginPath(); x.moveTo(PAD,140); x.lineTo(W-PAD,140); x.stroke();
    x.fillStyle=C.goldL; x.font='500 24px "DM Mono"'; sp(x,'PROMISE SCORECARD',7,W/2,430,'center');
    x.fillStyle=C.paper; x.textAlign='center';
    var size=fit(x,d.name,W-PAD*2,'900','Playfair Display',150,90); x.font='900 '+size+'px "Playfair Display"';
    var lines=wrap(x,d.name,W-PAD*2), ny=lines.length>1?480:550;
    lines.forEach(function(l){ x.fillText(l,W/2,ny); ny+=size; });
    x.fillStyle=C.goldL; x.font='italic 700 40px "Playfair Display"'; x.fillText(d.sub,W/2,ny+44); x.textAlign='left';
    x.fillStyle='rgba(245,240,232,0.5)'; x.font='500 22px "DM Mono"'; sp(x,d.total+' PROMISES TRACKED',6,W/2,H-180,'center');
    x.fillStyle=C.gold; x.fillRect(W/2-40,H-150,80,5);
    x.fillStyle=C.paper; x.font='500 22px "DM Mono"'; sp(x,'SWIPE →',6,W/2,H-95,'center'); return o.cv;
  }
  function aboutSlide(){ var o=newCanvas(C.paper), x=o.ctx; topbar(x);
    x.fillStyle=C.gold; x.font='500 22px "DM Mono"'; sp(x,'WHAT THIS IS',5,PAD,140,'left');
    x.fillStyle=C.ink; x.textAlign='left'; x.font='900 76px "Playfair Display"';
    x.fillText('Every politician',PAD,260); x.fillText('makes promises.',PAD,344);
    x.fillStyle=C.gold; x.fillText('We track the record.',PAD,448);
    x.fillStyle=C.ink; x.font='300 36px "DM Sans"'; var y=560;
    wrap(x,'PolicyLogic scores elected officials on the commitments they made to voters — public records, one consistent methodology, applied equally across parties.',W-PAD*2).forEach(function(l){ x.fillText(l,PAD,y); y+=52; });
    x.fillStyle=C.gold; x.fillRect(PAD,y+24,6,128);
    x.fillStyle=C.ink; x.font='italic 700 42px "Playfair Display"'; var y2=y+78;
    wrap(x,'We surface the evidence. You reach the conclusion.',W-PAD*2-44).forEach(function(l){ x.fillText(l,PAD+34,y2); y2+=54; });
    x.fillStyle=C.gray; x.font='500 20px "DM Mono"'; sp(x,'NO SPIN · NO ADVERTISERS · OPEN METHODOLOGY',3,PAD,H-80,'left'); return o.cv;
  }
  function methodSlide1(){ var o=newCanvas(C.paper), x=o.ctx; topbar(x);
    x.fillStyle=C.gold; x.font='500 22px "DM Mono"'; sp(x,'HOW SCORING WORKS',5,PAD,140,'left');
    x.fillStyle=C.ink; x.font='900 78px "Playfair Display"'; x.textAlign='left'; x.fillText('Three questions,',PAD,248); x.fillText('every promise.',PAD,332);
    x.strokeStyle=C.ink; x.lineWidth=3; x.beginPath(); x.moveTo(PAD,374); x.lineTo(W-PAD,374); x.stroke();
    var axes=[['01','Did they deliver?','Scored from D0 (no action) to D4 (delivered). This carries the most weight.'],
              ['02','Was it hard?','An easy win and a structural overhaul are not the same achievement.'],
              ['03','Did it matter?','How much was at stake — who, and how many, the promise affects.']];
    var y=450; axes.forEach(function(a){
      x.fillStyle=C.gold; x.font='900 60px "Playfair Display"'; x.fillText(a[0],PAD,y+4);
      x.fillStyle=C.ink; x.font='700 42px "DM Sans"'; x.fillText(a[1],PAD+120,y-10);
      x.fillStyle=C.gray; x.font='300 30px "DM Sans"'; wrap(x,a[2],W-PAD*2-120).forEach(function(l,i){ x.fillText(l,PAD+120,y+34+i*40); });
      y+=192; }); return o.cv;
  }
  function methodSlide2(){ var o=newCanvas(C.ink), x=o.ctx; topbar(x);
    x.fillStyle=C.goldL; x.font='500 22px "DM Mono"'; sp(x,'HOW TO READ A SCORE',5,PAD,140,'left');
    x.fillStyle=C.paper; x.font='900 78px "Playfair Display"'; x.textAlign='left'; x.fillText('Three things to',PAD,248); x.fillText('keep in mind.',PAD,332);
    var pts=[['Stakes don\u2019t drop when a promise fails.','A miss on something that mattered still counts as high-stakes. Failure doesn\u2019t shrink what was at risk.'],
             ['Difficulty only counts if delivered.','Promising something hard earns nothing on its own. No credit for ambition without progress.'],
             ['The number sits below the record.','A score summarizes the evidence — it never replaces it. Read the promise, not just the bucket.']];
    var y=460; pts.forEach(function(p){
      x.fillStyle=C.gold; x.fillRect(PAD,y-36,44,6);
      x.fillStyle=C.paper; x.font='700 42px "DM Sans"'; var hl=wrap(x,p[0],W-PAD*2); hl.forEach(function(l,i){ x.fillText(l,PAD,y+i*48); });
      var yy=y+hl.length*48+8;
      x.fillStyle='rgba(245,240,232,0.6)'; x.font='300 30px "DM Sans"'; wrap(x,p[1],W-PAD*2).forEach(function(l){ x.fillText(l,PAD,yy); yy+=40; });
      y=yy+44; }); return o.cv;
  }
  function statsSlide(d){ var o=newCanvas(C.paper), x=o.ctx; topbar(x);
    x.fillStyle=C.gold; x.font='500 22px "DM Mono"'; sp(x,'THE OFFICIAL',5,PAD,130,'left');
    x.fillStyle=C.ink; x.font='900 84px "Playfair Display"'; x.textAlign='left'; x.fillText('At a glance',PAD,225);
    x.strokeStyle=C.ink; x.lineWidth=3; x.beginPath(); x.moveTo(PAD,262); x.lineTo(W-PAD,262); x.stroke();
    var colX=[PAD,W/2+24], statY=350;
    [['IN OFFICE SINCE',d.since,C.ink],['PROMISES TRACKED',d.total,C.gold]].forEach(function(s,i){
      x.fillStyle=C.gray; x.font='500 20px "DM Mono"'; sp(x,s[0],3,colX[i],statY,'left');
      x.fillStyle=s[2]; x.font='900 150px "Playfair Display"'; x.fillText(s[1],colX[i]-6,statY+150); });
    x.strokeStyle=C.rule; x.lineWidth=2; x.beginPath(); x.moveTo(W/2,statY-30); x.lineTo(W/2,statY+170); x.stroke();
    var ry=680; [d.s1,d.s2,['Website',d.web]].forEach(function(row,i){
      x.fillStyle=C.gray; x.font='500 20px "DM Mono"'; x.textAlign='left'; sp(x,String(row[0]).toUpperCase(),2,PAD,ry,'left');
      x.fillStyle=i===2?C.gold:C.ink; x.font=(i===2?'500':'700')+' 38px "DM Sans"'; x.textAlign='right'; x.fillText(row[1],W-PAD,ry+4); x.textAlign='left';
      if(i<2){ x.strokeStyle=C.rule; x.lineWidth=1; x.beginPath(); x.moveTo(PAD,ry+36); x.lineTo(W-PAD,ry+36); x.stroke(); } ry+=98; }); return o.cv;
  }
  function promiseSlide(p,idx,shown){ var o=newCanvas(C.paper), x=o.ctx;
    var accent=TYPE_COLOR[p.type]||C.gold; topbar(x,accent);
    x.font='500 22px "DM Mono"'; var pill=String(p.type).toUpperCase(); var pw=x.measureText(pill).width+(pill.length*3)+44;
    x.fillStyle=accent; x.fillRect(PAD,86,pw,46); x.fillStyle=C.paper; sp(x,pill,3,PAD+22,116,'left');
    x.fillStyle=C.gray; x.font='italic 400 26px "DM Sans"'; x.fillText(TYPE_GLOSS[p.type]||'',PAD+pw+18,118);
    x.fillStyle=C.gray; x.font='500 22px "DM Mono"'; sp(x,idx+' / '+shown,4,W-PAD,116,'right');
    x.fillStyle=C.ink; x.textAlign='left'; var qs=60; x.font='700 '+qs+'px "Playfair Display"';
    var lines=wrap(x,'\u201C'+p.text+'\u201D',W-PAD*2);
    while(lines.length>4 && qs>42){ qs-=4; x.font='700 '+qs+'px "Playfair Display"'; lines=wrap(x,'\u201C'+p.text+'\u201D',W-PAD*2); }
    var py=232+qs; lines.forEach(function(l){ x.fillText(l,PAD,py); py+=qs*1.26; });
    var axY=H-360; x.strokeStyle=C.rule; x.lineWidth=2; x.beginPath(); x.moveTo(PAD,axY-30); x.lineTo(W-PAD,axY-30); x.stroke();
    var colW=(W-PAD*2)/3;
    var axes=[['DELIVERY',p.delivery,DELIVERY[p.delivery]||''],['DIFFICULTY',p.diff,DIFF[p.diff]||''],['IMPACT',p.impact,'at stake']];
    axes.forEach(function(a,i){ var ax=PAD+i*colW;
      x.fillStyle=C.gray; x.font='500 19px "DM Mono"'; sp(x,a[0],2,ax,axY+14,'left');
      x.fillStyle=i===0?accent:C.ink; x.font='900 64px "Playfair Display"'; x.fillText(a[1],ax,axY+84);
      x.fillStyle=C.gray; x.font='300 24px "DM Sans"'; wrap(x,a[2],colW-24).forEach(function(l,j){ x.fillText(l,ax,axY+122+j*30); });
      if(i>0){ x.strokeStyle=C.rule; x.lineWidth=1; x.beginPath(); x.moveTo(ax-22,axY-6); x.lineTo(ax-22,axY+132); x.stroke(); } });
    x.fillStyle=C.ink; x.font='500 22px "DM Mono"'; sp(x,'PROMISE SCORE',3,PAD,H-118,'left');
    x.fillStyle=accent; x.font='700 34px "DM Sans"'; x.textAlign='left'; x.fillText(p.score+' / 25',PAD+262,H-114);
    x.fillStyle=C.gray; x.font='italic 300 26px "DM Sans"'; x.fillText(p.detail||'',PAD,H-70); return o.cv;
  }
  function sourcesSlide(d,shown){ var o=newCanvas(C.ink), x=o.ctx; topbar(x);
    x.fillStyle=C.goldL; x.font='500 22px "DM Mono"'; sp(x,'THE RECORD',5,PAD,130,'left');
    x.fillStyle=C.paper; x.font='900 84px "Playfair Display"'; x.textAlign='left'; x.fillText('Sources',PAD,225);
    var sy=320; (d.sources||[]).forEach(function(s){ x.fillStyle=C.gold; x.fillRect(PAD,sy-26,8,8);
      x.fillStyle=C.paper; x.font='300 34px "DM Sans"'; var ls=wrap(x,s,W-PAD*2-30); ls.forEach(function(l,j){ x.fillText(l,PAD+30,sy); sy+=j<ls.length-1?46:0; });
      x.strokeStyle='rgba(245,240,232,0.18)'; x.lineWidth=1; x.beginPath(); x.moveTo(PAD,sy+28); x.lineTo(W-PAD,sy+28); x.stroke(); sy+=74; });
    x.fillStyle='rgba(245,240,232,0.5)'; x.font='300 28px "DM Sans"'; var dy=sy+18;
    wrap(x,'All scorecards are AI-assisted drafts under human review. Errors are logged publicly.',W-PAD*2).forEach(function(l){ x.fillText(l,PAD,dy); dy+=38; });
    x.fillStyle=C.gold; x.fillRect(PAD,H-200,W-PAD*2,4);
    x.fillStyle='rgba(245,240,232,0.6)'; x.font='500 22px "DM Mono"'; sp(x,'SHOWING '+shown+' OF '+d.total+' · '+String(d.rule||'ranked by stakes').toUpperCase(),3,W/2,H-140,'center');
    x.fillStyle=C.goldL; sp(x,'FULL METHODOLOGY · POLICYLOGIC.IO',3,W/2,H-95,'center'); return o.cv;
  }

  function buildSlides(d){
    var used=(d.promises||[]).slice(0,MAX_PROMISES), shown=used.length, out=[];
    out.push(coverSlide(d), aboutSlide(), methodSlide1(), methodSlide2(), statsSlide(d));
    used.forEach(function(p,i){ out.push(promiseSlide(p,i+1,shown)); });
    out.push(sourcesSlide(d,shown));
    return out;
  }

  // ---- font readiness: canvas can't draw a font that isn't loaded yet ----
  function ensureFonts(){
    if (!document.fonts || !document.fonts.load) return Promise.resolve();
    return Promise.all(FONTS.map(function(f){ return document.fonts.load(f); }))
                  .then(function(){ return document.fonts.ready; })
                  .catch(function(){ /* fall back to whatever is available */ });
  }

  function canvasToBlob(cv){
    return new Promise(function(res){ cv.toBlob(function(b){ res(b); }, 'image/png'); });
  }

  function downloadFiles(files, slug){
    files.forEach(function(file, i){
      setTimeout(function(){
        var url=URL.createObjectURL(file), a=document.createElement('a');
        a.href=url; a.download=slug+'-slide-'+('0'+(i+1)).slice(-2)+'.png';
        document.body.appendChild(a); a.click(); document.body.removeChild(a);
        setTimeout(function(){ URL.revokeObjectURL(url); }, 1000);
      }, i*250);
    });
  }

  function slugify(s){ return String(s).toLowerCase().replace(/[^a-z0-9]+/g,'-').replace(/^-|-$/g,''); }

  function generate(config, opts){
    opts=opts||{};
    return ensureFonts().then(function(){
      var canvases=buildSlides(config);
      return Promise.all(canvases.map(canvasToBlob)).then(function(blobs){
        var slug=slugify(config.name||'policylogic');
        var files=blobs.map(function(b,i){ return new File([b], slug+'-slide-'+('0'+(i+1)).slice(-2)+'.png', { type:'image/png' }); });

        // Prefer native share sheet with files (mobile). Fall back to download (desktop / unsupported).
        var canShareFiles = navigator.canShare && navigator.canShare({ files: files });
        if (canShareFiles && navigator.share){
          return navigator.share({
            files: files,
            title: (config.name||'PolicyLogic')+' — promise scorecard',
            text: (config.name||'')+' · tracked on PolicyLogic.io'
          }).catch(function(err){
            // user cancelled, or share failed mid-flight → silently fall back to download
            if (err && err.name === 'AbortError') return;
            downloadFiles(files, slug);
          });
        }
        downloadFiles(files, slug);
      });
    });
  }

  function attach(selector, config, opts){
    var btn = typeof selector==='string' ? document.querySelector(selector) : selector;
    if (!btn) { console.warn('PolicyLogicShare: button not found for', selector); return; }
    btn.addEventListener('click', function(){
      var orig=btn.textContent, was=btn.disabled;
      btn.disabled=true; btn.textContent='Preparing…';
      generate(config, opts).then(function(){
        btn.disabled=was; btn.textContent=orig;
      }).catch(function(e){
        console.error('PolicyLogicShare error:', e);
        btn.disabled=was; btn.textContent=orig;
        alert('Sorry — could not generate the share images. Please try again.');
      });
    });
  }

  global.PolicyLogicShare = { attach: attach, generate: generate, buildSlides: buildSlides };
})(window);
