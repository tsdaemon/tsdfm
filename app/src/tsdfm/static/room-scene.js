// Presentation only: state in, action callbacks out. No sockets or playback logic.
// Shadow DOM keeps scene illustration styles separate from the application theme.
(() => {
const stylesheetURL = new URL("room-scene.css", document.currentScript.src);
stylesheetURL.search = new URL(document.currentScript.src).search;
window.createRoomScene = function createRoomScene(host, actions) {
  const root = host.attachShadow({mode: "open"});
  const stylesheet = document.createElement("link");
  stylesheet.rel = "stylesheet";
  stylesheet.href = stylesheetURL.href;
  root.append(stylesheet);
  const shell = document.createElement("div");
  shell.innerHTML = `<section class="room" aria-label="Dancefloor and bar">
  <div class="scene" id="scene"><div class="backline"></div><div class="floor"></div><div class="eq" id="eq" aria-hidden="true"></div><div class="bar-sign">BAR</div><div class="shelf"></div><div class="bottles" aria-hidden="true"><i class="bottle"></i><i class="bottle"></i><i class="bottle"></i><i class="bottle"></i><i class="bottle"></i><i class="bottle"></i></div><div class="bartender" aria-label="Bartender"><svg viewBox="0 0 60 80" aria-hidden="true"><path d="M17 76L20 42Q30 36 40 42L45 76" fill="var(--text)"/><path d="M23 43L30 54L37 43L39 77H21Z" fill="var(--accent)"/><path d="M27 44L30 47L33 44" fill="none" stroke="var(--accent)" stroke-width="3"/><rect x="26" y="31" width="8" height="10" rx="3" fill="#c99572"/><ellipse cx="30" cy="23" rx="12" ry="15" fill="#d9a884"/><path d="M18 23V14Q30 0 42 14V23L37 14L21 17Z" fill="#40362f"/><path d="M24 30Q30 36 36 30" fill="none" stroke="#6b4836" stroke-width="2"/><circle cx="25" cy="24" r="1"/><circle cx="35" cy="24" r="1"/><g class="mixing"><path d="M21 46L14 55L30 59M39 46L46 55L33 60" fill="none" stroke="var(--text)" stroke-width="7" stroke-linecap="round"/><path d="M27 48H36L34 65H29Z" fill="#b8c4be"/></g></svg></div><button class="bar" id="bar-open" aria-expanded="false" aria-controls="bar-menu" title="Choose a drink at the bar"><span>ORDER A DRINK ↗</span></button><div id="stools"></div><div id="people"></div></div>
  <div class="controls" aria-label="Your character"><button data-move="headbang" aria-pressed="false">↯ Headbang</button><button data-move="jumping" aria-pressed="true">↑ Jumping</button><button data-move="hands" aria-pressed="false">✦ Disco</button><span class="separator"></span><button data-move="seated" aria-pressed="false">♧ Sit at bar</button></div>
  <section id="bar-menu" class="bar-menu" hidden aria-label="Bar drinks"><div class="menu-heading"><strong>Drinks</strong><button id="bar-close" aria-label="Close drink menu">✕</button></div><div class="drink-options" id="drink-options"></div></section><div class="status" id="status" role="status"></div>
 </section>`;
  root.append(shell);
  const $ = id => root.getElementById(id);
  const escape = value => String(value).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const menu=[
 {id:'beer',name:'Lager',color:'#e9bb4f',type:'beer'},
 {id:'ipa',name:'IPA',color:'#cc7c32',type:'beer'},
 {id:'stout',name:'Stout',color:'#392c28',type:'beer'},
 {id:'whisky',name:'Whisky',color:'#be8039',type:'rocks'},
 {id:'rum',name:'Rum',color:'#814830',type:'rocks'},
 {id:'gin',name:'Gin',color:'#c4e2c6',type:'stem'},
 {id:'vodka',name:'Vodka',color:'#d9e9ee',type:'shot'},
 {id:'tequila',name:'Tequila',color:'#d4db86',type:'shot'},
 {id:'coffee',name:'Coffee',color:'#724830',type:'rocks'},
 {id:'water',name:'Water',color:'#a7d4e5',type:'rocks'},
 {id:'',name:'Put down',color:'',type:'none'}
];
function drinkArt(id){
 const d=menu.find(x=>x.id===id);if(!d||!id)return '';
 const glasses={
 beer:`<path d="M29 15H34Q42 15 40 27Q39 32 30 32" fill="none" stroke="var(--muted)" stroke-width="3"/><path d="M10 11H31L29 42H12Z" fill="${d.color}" stroke="var(--muted)" stroke-width="1.5"/><path d="M11 13H30V20H11Z" fill="#f5e9c7"/>`,
 rocks:`<path d="M8 19H34L31 41H11Z" fill="#dce6de33" stroke="var(--muted)" stroke-width="1.5"/><path d="M11 28H31L29 39H13Z" fill="${d.color}"/><path d="M15 23L22 22L24 29L17 30ZM25 25L30 27L28 33L23 31Z" fill="#d8ecec99"/>`,
 stem:`<path d="M5 9H37L21 28Z" fill="${d.color}" stroke="var(--muted)" stroke-width="1.5"/><path d="M21 28V42M13 43H29" stroke="var(--muted)" stroke-width="2"/><path d="M27 6L17 21" stroke="#aa8662"/><circle cx="21" cy="15" r="3" fill="#7f944a"/>`,
 shot:`<path d="M12 16H30L28 41H14Z" fill="#dce6de33" stroke="var(--muted)" stroke-width="1.5"/><path d="M15 25H27L26 38H16Z" fill="${d.color}"/>${id==='tequila'?'<path d="M30 13A7 7 0 0 1 39 23L30 23Z" fill="#a9ce61" stroke="#d9e7a3"/>':''}`};
 return `<svg viewBox="0 0 44 48" aria-hidden="true">${glasses[d.type]}</svg>`;
}

function figure(p){return `<svg class="figure" viewBox="0 0 60 80" aria-hidden="true"><ellipse cx="30" cy="77" rx="16" ry="3" fill="#000" opacity=".15"/><g class="legs" fill="none" stroke="var(--muted)" stroke-width="7" stroke-linecap="round"><path d="M25 56L23 72"/><path d="M35 56L38 72"/></g><g class="torso"><path d="M21 36Q30 32 39 36L41 57Q30 61 19 57Z" fill="${p.color}"/><g fill="none" stroke="${p.color}" stroke-width="7" stroke-linecap="round"><g class="arm-l"><path d="M21 39L13 47L10 41"/><path class="disco-finger" d="M10 41L7 34" stroke="#e5bc9c" stroke-width="3"/></g><g class="arm-r"><path d="M39 39L46 47L50 40"/><path class="disco-finger" d="M50 40L53 33" stroke="#e5bc9c" stroke-width="3"/><path class="horns" d="M47 40L46 33M53 40L55 34" stroke="#e5bc9c" stroke-width="2.5"/></g></g><g class="head"><path class="metal-hair" d="M18 17Q4 29 12 49L23 37L28 16M35 13Q52 21 47 48L35 36Z" fill="${p.hair}"/><text x="30" y="32" text-anchor="middle" font-size="26">${escape(p.avatar || "🙂")}</text></g></g></svg>`}

  const nodes = new Map(); // Render-owned element handles, never application state.
  const drinkButtons = new Map();
  const seats = [72, 82, 92];
  seats.forEach(x => {const stool=document.createElement('div');stool.className='stool';stool.style.left=`calc(${x}% - 14px)`;$('stools').append(stool)});
  const bars = Array.from({length:22}, () => {
    const bar = document.createElement('span');
    $('eq').append(bar);
    return bar;
  });
  function renderSpectrum(state) {
    const live = state.phase === 'live' && state.view === 'room' && !!state.nowPlaying?.on_air;
    bars.forEach((bar, index) => {
      const level = live ? state.spectrum?.levels[index] || 0 : 0;
      bar.style.setProperty('--level', Math.max(0.045, Math.min(1, level)));
    });
    $('scene').style.setProperty('--energy', live ? state.spectrum?.energy || 0 : 0);
  }
  root.querySelectorAll('[data-move]').forEach(button => button.addEventListener('click', () => actions.choose({move:button.dataset.move})));
  $('bar-open').addEventListener('click', actions.toggleBar);
  $('bar-close').addEventListener('click', actions.closeBar);
  root.addEventListener('keydown', event => {if(event.key==='Escape') actions.closeBar()});
  for (const drink of menu) {
    const button=document.createElement('button');button.type='button';
    button.innerHTML=drinkArt(drink.id)+`<span>${drink.name}</span>`;
    button.addEventListener('click',()=>actions.choose({drink:drink.id}));
    $('drink-options').append(button);drinkButtons.set(drink.id,button);
  }
  function renderScene(state) {
    renderSpectrum(state);
    const users=state.listeners || [];
    const me=users.find(user=>user.id===state.me.id);
    const mine=me?.scene || {move:'hands',drink:''};
    const active=state.phase==='live' && state.view==='room';
    const playing=active && !!state.nowPlaying?.on_air;
    const bpm=Number(state.nowPlaying?.bpm);
    const known=Number.isFinite(bpm) && bpm>=20 && bpm<=300;
    host.style.setProperty('--beat',`${60/(known?bpm:120)}s`);
    $('scene').classList.toggle('paused',!active);
    $('scene').classList.toggle('idle',!playing);
    $('bar-menu').hidden=!state.sceneBarOpen;
    $('bar-open').setAttribute('aria-expanded',String(!!state.sceneBarOpen));
    $('bar-open').disabled=state.phase!=='live';
    root.querySelectorAll('[data-move]').forEach(button=>{
      button.setAttribute('aria-pressed',String(button.dataset.move===mine.move));
      button.disabled=state.phase!=='live';
    });
    for(const [id,button] of drinkButtons){button.setAttribute('aria-pressed',String(id===mine.drink));button.disabled=state.phase!=='live'}
    $('status').textContent=state.sceneError || '';
    $('status').hidden=!state.sceneError;
    const seated=users.filter(u=>u.scene?.move==='seated').sort((a,b)=>a.id.localeCompare(b.id));
    const dancers=users.filter(u=>u.scene?.move!=='seated').sort((a,b)=>a.id.localeCompare(b.id));
    const rows=Math.ceil(dancers.length/4);
    $('scene').style.height=`${Math.max(365,240+rows*82)}px`;
    const present=new Set(users.map(user=>user.id));
    for(const [id,node] of nodes){if(!present.has(id)){node.el.remove();nodes.delete(id)}}
    for (const user of users) {
      let node=nodes.get(user.id);
      if(!node){
        const el=document.createElement('div'), art=document.createElement('span'), icon=document.createElement('span'),name=document.createElement('span');
        icon.className='drink-icon';name.className='name';el.append(art,icon,name);$('people').append(el);
        node={el,art,icon,name,avatar:undefined,drink:null};nodes.set(user.id,node);
      }
      const social=user.scene||{move:'hands',drink:''};
      const move=['headbang','jumping','hands','seated'].includes(social.move)?social.move:'hands';
      const sit=move==='seated',index=(sit?seated:dancers).findIndex(u=>u.id===user.id);
      const pose=sit?'seated':playing?move:'resting';
      node.el.className=`person ${pose}${social.drink?' has-drink':''}${user.id===state.me.id?' self':''}`;
      node.el.style.left=`${sit?seats[index%3]:13+(index%4)*14}%`;
      node.el.style.top=`${sit?199:177+Math.floor(index/4)*82+(index%2)*15}px`;
      node.el.style.zIndex=String(sit?4:3+Math.floor(index/4));
      if(node.avatar!==user.avatar){
        let hash=0;for(const c of user.id)hash=(hash*31+c.charCodeAt(0))>>>0;
        node.art.innerHTML=figure({avatar:user.avatar,color:`color-mix(in srgb, var(--accent) ${35+(hash%5)*12}%, var(--text))`,hair:'#51443b'});
        node.avatar=user.avatar;
      }
      if(node.drink!==social.drink){node.icon.innerHTML=drinkArt(social.drink);node.drink=social.drink}
      node.name.textContent=user.name;
      node.el.setAttribute('aria-label',`${user.name}: ${sit?'seated':!playing?'relaxing':move==='hands'?'disco':move}${social.drink?', '+(menu.find(d=>d.id===social.drink)?.name||'drink'):''}`);
      node.el.title=`${user.name}${social.drink?' · '+(menu.find(d=>d.id===social.drink)?.name||'drink'):''}`;
    }
  }
  renderScene.spectrum = renderSpectrum;
  return renderScene;
};

})();
