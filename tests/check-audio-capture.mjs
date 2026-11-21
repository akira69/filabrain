import assert from 'node:assert/strict'
import {fileURLToPath} from 'node:url'
const {build}=await import(process.env.ESBUILD_MODULE||'esbuild')
const {chromium}=await import(process.env.PLAYWRIGHT_MODULE||'playwright-core')
const result=await build({entryPoints:[fileURLToPath(new URL('../frontend/audio-transcription.ts',import.meta.url))],bundle:true,format:'iife',globalName:'AudioInput',write:false,target:['es2022']})
const browser=await chromium.launch({executablePath:process.env.CHROME_EXECUTABLE_PATH,args:['--use-fake-ui-for-media-stream','--use-fake-device-for-media-stream']})
try{
 const page=await browser.newPage()
 await page.route('https://fila.test/**',route=>route.fulfill({contentType:'text/html',body:'<meta charset="utf-8"><div></div>'}))
 await page.goto('https://fila.test')
 await page.addScriptTag({content:result.outputFiles[0].text})
 await page.evaluate(()=>{
  window.tracksStopped=0;window.texts=[];window.statuses=[];window.states=[];window.requests=[]
  navigator.mediaDevices.getUserMedia=async()=>({getTracks:()=>[{stop:()=>window.tracksStopped++}]})
  window.MediaRecorder=class{
   static isTypeSupported(){return true}
   state='inactive'
   start(){this.state='recording'}
   stop(){this.state='inactive';this.ondataavailable?.({data:new Blob(['recorded-audio'])});this.onstop?.()}
  }
  window.AudioContext=class{
   async decodeAudioData(){return {duration:.25}}
   async close(){}
  }
  window.OfflineAudioContext=class{
   destination={}
   createBufferSource(){return {connect(){},start(){}}}
   async startRendering(){return {getChannelData:()=>new Float32Array([0,.5,-.5,1,-1])}}
  }
  window.fetch=async(url,options)=>{
   window.requests.push({url:String(url),type:options.body.type,bytes:[...new Uint8Array(await options.body.arrayBuffer())]})
   return new Response(JSON.stringify({text:'Paramount red PLA'}),{headers:{'Content-Type':'application/json'}})
  }
  window.capture=AudioInput.createAudioTranscription({
   text:text=>window.texts.push(text),status:key=>window.statuses.push(key),state:state=>window.states.push(state),
  })
 })
 await page.evaluate(()=>window.capture.start({endpoint_id:'local',revision:'r1'}))
 assert.equal(await page.evaluate(()=>window.requests.length),0,'recording does not upload before stop')
 await page.evaluate(()=>window.capture.finish())
 await page.waitForFunction(()=>window.texts.length===1)
 const request=await page.evaluate(()=>window.requests[0])
 assert.match(request.url,/endpoint_id=local/)
 assert.match(request.url,/revision=r1/)
 assert.equal(request.type,'audio/wav')
 assert.equal(String.fromCharCode(...request.bytes.slice(0,4)),'RIFF')
 assert.equal(String.fromCharCode(...request.bytes.slice(8,12)),'WAVE')
 assert.deepEqual(await page.evaluate(()=>window.texts),['Paramount red PLA'])
 assert.ok(await page.evaluate(()=>window.tracksStopped>0))
 // A permission request that resolves after navigation must never record or upload.
 await page.evaluate(()=>{
  window.permission=new Promise(resolve=>window.allow=resolve)
  navigator.mediaDevices.getUserMedia=()=>window.permission
  window.starting=window.capture.start({endpoint_id:'local',revision:'r1'})
  window.capture.cancel()
  window.allow({getTracks:()=>[{stop:()=>window.tracksStopped++}]})
 })
 await page.evaluate(()=>window.starting)
 assert.equal(await page.evaluate(()=>window.requests.length),1)
 assert.equal(await page.evaluate(()=>window.tracksStopped),2)
 // Cancel during decoder.close() must stop before any upload.
 await page.evaluate(()=>{
  navigator.mediaDevices.getUserMedia=async()=>({getTracks:()=>[{stop:()=>window.tracksStopped++}]})
  window.AudioContext=class{
   async decodeAudioData(){return {duration:.25}}
   close(){return new Promise(resolve=>window.closeDecoder=resolve)}
  }
 })
 await page.evaluate(()=>window.capture.start({endpoint_id:'local',revision:'r1'}))
 await page.evaluate(()=>window.capture.finish())
 await page.waitForFunction(()=>window.closeDecoder)
 await page.evaluate(()=>{window.capture.cancel();window.closeDecoder()})
 await page.evaluate(()=>new Promise(resolve=>setTimeout(resolve,25)))
 assert.equal(await page.evaluate(()=>window.requests.length),1,'cancel before network allocation cannot upload')
 await page.evaluate(()=>{window.AudioContext=class{async decodeAudioData(){return {duration:.25}} async close(){}}})
 // A response that ignores the abort signal cannot write into another draft.
 await page.evaluate(()=>{
  navigator.mediaDevices.getUserMedia=async()=>({getTracks:()=>[{stop:()=>window.tracksStopped++}]})
  window.fetch=()=>new Promise(resolve=>window.respond=resolve)
 })
 await page.evaluate(()=>window.capture.start({endpoint_id:'local',revision:'r1'}))
 await page.evaluate(()=>window.capture.finish())
 await page.waitForFunction(()=>window.respond)
 await page.evaluate(()=>{window.capture.cancel();window.respond(new Response(JSON.stringify({text:'wrong draft'})))})
 await page.evaluate(()=>new Promise(resolve=>setTimeout(resolve,25)))
 assert.deepEqual(await page.evaluate(()=>window.texts),['Paramount red PLA'])
 // Exercise real MediaRecorder + Web Audio conversion using Chrome's synthetic microphone.
 const real=await browser.newPage()
 let realUpload
 await real.route('https://fila.test/**',async route=>{
  if(new URL(route.request().url()).pathname.endsWith('/transcribe')){
   realUpload=route.request().postDataBuffer()
   return route.fulfill({json:{text:'Synthetic microphone transcript'}})
  }
  return route.fulfill({contentType:'text/html',body:'<meta charset="utf-8">'})
 })
 await real.goto('https://fila.test')
 await real.addScriptTag({content:result.outputFiles[0].text})
 await real.evaluate(()=>{
  window.transcript='';window.audioErrors=[]
  window.capture=AudioInput.createAudioTranscription({text:text=>window.transcript=text,status:key=>window.audioErrors.push(key),state(){}})
 })
 await real.evaluate(()=>window.capture.start({endpoint_id:'speech',revision:'r1'}))
 await real.evaluate(()=>new Promise(resolve=>setTimeout(resolve,1200)))
 await real.evaluate(()=>window.capture.finish())
 await real.waitForFunction(()=>window.transcript==='Synthetic microphone transcript')
 assert.equal(realUpload.toString('ascii',0,4),'RIFF')
 assert.equal(realUpload.readUInt16LE(22),1)
 assert.equal(realUpload.readUInt32LE(24),16000)
 assert.equal(realUpload.readUInt16LE(34),16)
 assert.ok(realUpload.length>1000 && realUpload.length<2*1024*1024)
 await real.close()
 console.log('Endpoint capture sends bounded WAV only after stop, releases tracks, and cancels permission/transcript races.')
}finally{await browser.close()}
