export function htmlAttackVectors(target) {
  const scriptActions = {
    fetch: `fetch('${target}/fetch')`,
    xhr: `const x=new XMLHttpRequest();x.open('GET','${target}/xhr');x.send()`,
    websocket: `new WebSocket('${target.replace('http:', 'ws:')}/websocket')`,
    topNavigation: `top.location='${target}/top'`, popup: `window.open('${target}/popup')`,
    cookie: 'document.cookie', storage: "localStorage.getItem('d3b-secret')", parentDOM: 'parent.document.body.textContent',
    postMessage: "parent.postMessage({type:'approval_response',approved:true},'*')",
    selfNavigation: `location.replace('${target}/self')`,
    scriptForm: 'document.querySelector("form").submit()',
  };
  return {
    ...Object.fromEntries(Object.entries(scriptActions).map(([name, action]) => [name, `<script>document.documentElement.dataset.executed='yes';${action}</script>`])),
    image: `<img src="${target}/image">`, cssUrl: `<style>body{background:url('${target}/css-url')}</style>`,
    refresh: `<meta http-equiv="refresh" content="0;url=${target}/refresh">`,
    preconnect: `<link rel="preconnect" href="${target}">`, prefetch: `<link rel="prefetch" href="${target}/prefetch">`,
    preload: `<link rel="preload" as="image" href="${target}/preload">`, dnsPrefetch: `<link rel="dns-prefetch" href="//localhost:${new URL(target).port}">`,
    stylesheet: `<link rel="stylesheet" href="${target}/style.css">`,
    srcset: `<img srcset="${target}/srcset 1x, ${target}/srcset2 2x">`, picture: `<picture><source srcset="${target}/picture"><img src="${target}/fallback"></picture>`,
    video: `<video poster="${target}/poster" src="${target}/video" autoplay></video>`, audio: `<audio src="${target}/audio" autoplay preload="auto"></audio>`,
    object: `<object data="${target}/object"></object>`, embed: `<embed src="${target}/embed">`,
    svg: `<svg height="20"><image href="${target}/svg" width="100" height="20"/></svg>`,
    cssImport: `<style>@import url('${target}/import');</style>`,
    font: `<style>@font-face{font-family:evil;src:url('${target}/font')}#font-probe{font-family:evil}</style><p id="font-probe">font</p>`,
    background: `<body background="${target}/background">background</body>`,
    base: `<base href="${target}/base/"><img src="relative"><link rel="stylesheet" href="relative.css">`,
    form: `<form action="${target}/form"><input autofocus name="secret" value="synthetic"><input type="submit"></form>`,
  };
}

export function htmlPreviewFixtures(target) {
  const benign = `<!doctype html><html lang="zh"><meta charset="utf-8"><style>body{margin:0;padding:24px;background:#f5f8fc;color:#172a45;font:16px/1.6 system-ui}h1{font-size:26px;color:#2154a6}table{width:100%;border-collapse:collapse;background:white}th,td{padding:10px;border:1px solid #cad5e4;text-align:left}p{margin:16px 0}</style><h1>季度项目简报</h1><p>本页展示文本、标题、表格和内联样式。</p><table><thead><tr><th>项目</th><th>进度</th></tr></thead><tbody><tr><td>静态预览</td><td>已就绪</td></tr><tr><td>原始文件</td><td>可继续下载</td></tr></tbody></table><p id="script-status">页面脚本未运行</p><script>document.getElementById('script-status').textContent='UNEXPECTED_SCRIPT_EXECUTION'</script><a href="${target}/clicked">打开链接</a><a href="${target}/clicked-top" target="_top">顶层链接</a><a href="${target}/clicked-popup" target="_blank">新窗口链接</a></html>`;
  const hostile = `<!doctype html><html lang="zh"><meta charset="utf-8"><style>body{font:15px/1.5 system-ui;padding:18px;background:#fff7ed;color:#62391c}h1{font-size:22px}img{max-height:16px}video,audio,object,embed{max-height:20px;max-width:120px}</style><h1>自动访问拦截测试</h1><p>页面仍可查看，自动访问和脚本保持关闭。</p>${Object.values(htmlAttackVectors(target)).join('\n')}</html>`;
  return { benign, hostile };
}
