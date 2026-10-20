-- Compiled after bridge.lua, sharing its private dispatcher. No runtime eval.
local engineLog = {}
local logSequence = 0
local originalPrint = print
function print(...)
    local texts = {}
    for i=1,select("#",...) do texts[#texts+1]=tostring(select(i,...)) end
    local timed,now=pcall(getTime) -- top-level extension print can run off the audio thread
    logSequence=logSequence+1
    engineLog[#engineLog+1]={sequence=logSequence,time=timed and now or nil,message=table.concat(texts," ")}
    if #engineLog>256 then table.remove(engineLog,1) end
    originalPrint(...)
end

local function assertIdentity(node,a)
    if a.expected_path then check(node.path==a.expected_path,"Stale node identity; re-scan project") end
end

local function clean(value,depth)
    if depth>10 then return {unavailable="metadata nesting limit"} end
    local t=type(value)
    if t=="nil" or t=="string" or t=="boolean" then return value end
    if t=="number" then
        if value~=value or math.abs(value)==math.huge then return {unavailable="non-finite number"} end
        return value
    end
    if t=="table" then
        local out,count={},0
        for k,v in pairs(value) do
            count=count+1
            if count>512 then out.truncated=true;break end
            out[k]=clean(v,depth+1)
        end
        return out
    end
    return {unavailable=t}
end

local function allChildren(node,selector)
    local result,seen={},{}
    local function add(child,path,relation,index)
        local identity=child.path
        if not seen[identity] then
            seen[identity]=true
            local item=summary(child,path)
            item.relation=relation;item.index=index
            result[#result+1]=item
        end
    end
    local program=safeProperty(node,"program")
    if node.type=="Part" and program then add(program,selector..".program","program",1) end
    for _,field in ipairs({"parts","layers","keygroups","oscillators","inserts","auxs","sends","mods","eventProcessors","children"}) do
        local list=collection(node,field)
        if list then for i,child in ipairs(list) do add(child,selector.."."..field.."["..i.."]",field,i) end end
    end
    return result
end

function handlers.inspect_node(a)
    local node=resolve(a.selector)
    assertIdentity(node,a)
    local info=summary(node,a.selector)
    local errors={}
    for _,field in ipairs({"output","sampleInfo","looplabInfo","numSlices","purged"}) do
        local ok,value=pcall(function() return node[field] end)
        if ok and value~=nil then info[field]=clean(value,0) end
    end
    if node.type=="SampleMappingOscillator" then
        info.numZones=node:getNumZones();info.loadingStatus=node:getLoadingStatus()
    end
    local children=allChildren(node,a.selector)
    local out={}
    for i=(a.offset or 0)+1,math.min(#children,(a.offset or 0)+(a.limit or 50)) do out[#out+1]=children[i] end
    return {node=info,items=out,total_count=#children,has_more=(a.offset or 0)+#out<#children,
        next_offset=(a.offset or 0)+#out,errors=errors}
end

function handlers.parameter_page(a)
    local node=resolve(a.selector)
    assertIdentity(node,a)
    local defs=definitions(node)
    local out={}
    for i=(a.offset or 0)+1,math.min(#defs,(a.offset or 0)+(a.limit or 16)) do
        local d=defs[i]
        local ok,value=pcall(function() return node:getParameter(d.name) end)
        local item={definition=d}
        if ok then
            if type(value)=="string" and #value>2048 then
                item.long_string=true;item.byte_length=#value
            else item.value=clean(value,0) end
        else item.error=tostring(value) end
        local connected,connections=pcall(function() return node:getParameterConnections(d.name) end)
        if connected then item.connection_count=#(connections or {}) else item.connection_error=tostring(connections) end
        out[#out+1]=item
    end
    return {items=out,total_count=#defs,has_more=(a.offset or 0)+#out<#defs,next_offset=(a.offset or 0)+#out}
end

function handlers.value_chunk(a)
    local node=resolve(a.selector);assertIdentity(node,a);definition(node,a.parameter)
    local value=node:getParameter(a.parameter)
    check(type(value)=="string","Parameter is not a string")
    local first=(a.offset or 0)+1
    local last=math.min(#value,first+(a.limit or 4096)-1)
    -- Keep a UTF-8 codepoint intact at each chunk boundary.
    while last<#value and last>=first do
        local following=string.byte(value,last+1)
        if following>=128 and following<192 then last=last-1 else break end
    end
    return {text=string.sub(value,first,last),byte_length=#value,next_offset=last,has_more=last<#value}
end

function handlers.connections(a)
    local node=resolve(a.selector);assertIdentity(node,a);definition(node,a.parameter)
    local all=node:getParameterConnections(a.parameter) or {}
    local out={}
    for i=(a.offset or 0)+1,math.min(#all,(a.offset or 0)+(a.limit or 32)) do
        local c=all[i];local parameters={}
        for _,d in ipairs(definitions(c)) do
            local ok,value=pcall(function() return c:getParameter(d.name) end)
            local item={definition=d}
            if ok then item.value=clean(value,0) else item.error=tostring(value) end
            parameters[#parameters+1]=item
        end
        out[#out+1]={index=i,name=c.name,type=c.type,path=c.path,parameters=parameters}
    end
    return {items=out,total_count=#all,has_more=(a.offset or 0)+#out<#all,next_offset=(a.offset or 0)+#out}
end

function handlers.sample_info(a)
    local node=resolve(a.selector)
    local info={sampleInfo=clean(safeProperty(node,"sampleInfo"),0),
        looplabInfo=clean(safeProperty(node,"looplabInfo"),0),purged=safeProperty(node,"purged")}
    local count=safeProperty(node,"numSlices") or 0
    local out={}
    for i=(a.offset or 0)+1,math.min(count,(a.offset or 0)+(a.limit or 30)) do
        out[#out+1]={index=i,info=clean(node:getSliceInfo(i),0)}
    end
    info.items=out;info.total_count=count;info.has_more=(a.offset or 0)+#out<count;info.next_offset=(a.offset or 0)+#out
    if node.type=="SampleMappingOscillator" then info.numZones=node:getNumZones();info.loadingStatus=node:getLoadingStatus() end
    return info
end

function handlers.mapping(a)
    local node=resolve(a.selector)
    check(node.type=="SampleMappingOscillator","Mapping operations require SampleMappingOscillator")
    if a.action=="load" then
        return startTask(function(cb) return node:loadMapping(a.path,cb) end,
            function() return {numZones=node:getNumZones(),loadingStatus=node:getLoadingStatus()} end)
    elseif a.action=="clear" then node:clearMapping()
    elseif a.action=="purge" then node:purgeZones(a.dim1,a.dim2)
    elseif a.action=="unpurge" then
        return startTask(function(cb) return node:unpurgeZones(a.dim1,a.dim2,cb) end,
            function() return {numZones=node:getNumZones(),loadingStatus=node:getLoadingStatus()} end)
    else error("Unknown mapping operation") end
    return {numZones=node:getNumZones(),loadingStatus=node:getLoadingStatus()}
end
mutations.mapping=true

function handlers.impulse(a)
    local node=resolve(a.selector)
    return startTask(function(cb) return loadImpulse(node,a.path,cb) end,function() return {applied=true} end)
end
mutations.impulse=true

local queries={getTime=true,getTempo=true,getBeatTime=true,getRunningBeatTime=true,getBarDuration=true,
    getBeatDuration=true,getSamplingRate=true,getTimeSignature=true,getNoteDuration=true,getCC=true,
    isKeyDown=true,isOctaveKeyDown=true,findLayer=true,getLocation=true,
    beat2ms=true,ms2beat=true,ms2samples=true,samples2ms=true}
local voiceMethods={changeTune=true,changePan=true,changeVolume=true,changeVolumedB=true,setSampleOffset=true,
    fadeout=true,fadein=true,fade=true,fade2=true,releaseVoice=true,sendScriptModulation=true,sendScriptModulation2=true}

function handlers.api_info(a)
    local available={}
    for name,_ in pairs(queries) do available[name]=type(_G[name])=="function" end
    for name,_ in pairs(voiceMethods) do available[name]=type(_G[name])=="function" end
    for _,name in ipairs({"playNote","postEvent","controlChange","pitchBend","afterTouch","polyAfterTouch",
        "programChange","postMidiEvent","loadImpulse","loadMidi","createMidiFile","saveMidi","saveState","loadState"}) do
        available[name]=type(_G[name])=="function"
    end
    return {functions=available,version="0.2.0",extension="Native Lua extension supported at next script reload"}
end

function handlers.query_api(a)
    check(queries[a.method],"Not a supported query method")
    local result={_G[a.method](unpack(a.arguments or {}))}
    return {values=clean(result,0)}
end

-- Native VoiceId is userdata, which saveData silently drops from JSON. Keep it
-- in this Lua instance and expose an opaque, non-reusable handle instead.
local voiceHandles = {}
local nextVoiceHandle = 0
local voicePrefix=nil -- getTime requires Falcon's audio thread; initialize on first request
local function forgetVoice(handle)
    local entry=voiceHandles[handle]
    if not entry then return end
    local remaining={}
    for _,id in ipairs(voices) do if id~=entry.id then remaining[#remaining+1]=id end end
    voices=remaining;voiceHandles[handle]=nil
end
local function reserveVoice()
    local expired={}
    for handle,entry in pairs(voiceHandles) do
        if entry.expires and getTime()>entry.expires then expired[#expired+1]=handle end
    end
    for _,handle in ipairs(expired) do forgetVoice(handle) end
    check(#voices<256,"Too many tracked voices; release handles or call panic")
end
local function storeVoice(id,duration)
    if id==nil then return nil end
    if not voicePrefix then voicePrefix="voice-"..(bridgeInstance.."-"..getTime()):gsub("[^%w%-]", "").."-" end
    nextVoiceHandle=nextVoiceHandle+1
    local handle=voicePrefix..nextVoiceHandle
    voiceHandles[handle]={id=id,expires=duration and getTime()+duration+60000 or nil}
    voices[#voices+1]=id
    return handle
end
local function nativeVoice(handle)
    if handle==-1 then return -1 end -- only meaningful for APIs documenting all voices
    local entry=voiceHandles[handle]
    check(entry~=nil,"Unknown/expired voice handle; handles expire on script reload or release")
    return entry.id
end
local oldPanic=handlers.panic
function handlers.panic(a)
    local result=oldPanic(a);voiceHandles={};return result
end

function handlers.voice_start(a)
    reserveVoice()
    local id=playNote(a.note,a.velocity,a.duration_ms,a.layer,a.channel,a.input,a.volume,a.pan,a.tune,a.slice,a.osc_index,a.dim1,a.dim2)
    -- UVI reads the start offset exactly once, before this callback returns.
    if id~=nil and a.sample_offset_ms~=nil then setSampleOffset(id,a.sample_offset_ms) end
    return {voice_id=storeVoice(id,a.duration_ms),started=id~=nil,voice_id_type="opaque handle"}
end
mutations.voice_start=true
function handlers.voice_api(a)
    check(voiceMethods[a.method],"Unsupported voice method")
    local fn=_G[a.method];check(type(fn)=="function","Method unavailable in installed Falcon")
    local args=a.arguments or {}
    local index=1
    if a.method=="sendScriptModulation" then index=4 end
    if a.method=="sendScriptModulation2" then index=5 end
    local handle=args[index]
    if handle~=nil then args[index]=nativeVoice(handle) end
    local result=fn(unpack(args))
    if a.method=="releaseVoice" then forgetVoice(handle) end
    return {applied=true,result=clean(result,0)}
end
mutations.voice_api=true

function handlers.post_event(a)
    reserveVoice()
    local existing=a.event.id
    if existing~=nil then a.event.id=nativeVoice(existing) end
    local id=postEvent(a.event,a.delta)
    if existing~=nil then return {posted=true,voice_id=existing} end
    return {posted=true,voice_id=storeVoice(id,nil)}
end
mutations.post_event=true

function handlers.midi_extra(a)
    if a.kind=="poly_aftertouch" then polyAfterTouch(a.value,a.note,a.channel,a.input)
    elseif a.kind=="raw" then
        postMidiEvent(uvi.MidiEvent(a.timestamp,a.channel,a.status,a.byte1,a.byte2))
    else error("Unknown MIDI kind") end
    return {sent=true}
end
mutations.midi_extra=true

local midiObjects={}
local nextMidiId=0
function handlers.midi_file(a)
    local function store(sequence)
        nextMidiId=nextMidiId+1
        local count=0;for _ in pairs(midiObjects) do count=count+1 end
        check(count<16,"Release a MIDI handle before loading another file")
        local id=tostring(nextMidiId);midiObjects[id]=sequence
        return {handle=id,numTracks=sequence.numTracks,division=sequence.division,ppq=sequence.ppq,format=sequence.format}
    end
    if a.action=="load" or a.action=="create" then
        local holder={}
        return startTask(function(cb)
            local function done(t) holder.midi=t.midi;cb(t) end
            if a.action=="load" then return loadMidi(a.path,done) end
            return createMidiFile(a.max_tracks,a.max_events,done)
        end,function() return store(holder.midi) end)
    end
    local sequence=midiObjects[a.handle];check(sequence~=nil,"MIDI handle expired; reload file")
    if a.action=="release" then midiObjects[a.handle]=nil;return {released=true} end
    if a.action=="save" then
        return startTask(function(cb) return saveMidi(sequence,a.path,cb) end,function() return {saved=true} end)
    end
    if a.action=="insert" then
        local e=a.event
        sequence:insertEvent(a.track,uvi.MidiEvent(e.timestamp,e.channel,e.status,e.byte1,e.byte2))
        return {inserted=true}
    end
    if a.action=="find" then
        local allowed={findAt=true,findNextAt=true,findPreviousAt=true}
        check(allowed[a.method],"Unknown MIDI search")
        local index=sequence[a.method](sequence,a.track,a.position)
        return {index=index,found=index~=nil}
    end
    local count=sequence:getNumEventsForTrack(a.track)
    local out={}
    for i=(a.offset or 0)+1,math.min(count,(a.offset or 0)+(a.limit or 30)) do
        local e=sequence:getEvent(a.track,i)
        out[#out+1]={index=i,timestamp=e.timestamp,channel=e.channel,status=e.status,type=e.type,byte1=e.byte1,byte2=e.byte2}
    end
    return {items=out,total_count=count,next_offset=(a.offset or 0)+#out,has_more=(a.offset or 0)+#out<count}
end
mutations.midi_file=true

function handlers.script_state(a)
    return startTask(function(cb)
        if a.action=="save" then return saveState(a.path,cb) end
        return loadState(a.path,cb)
    end,function() return {applied=true,scope="ScriptProcessor state only; not a Program or Multi"} end)
end
mutations.script_state=true

function handlers.engine_logs(a)
    local out={}
    for _,entry in ipairs(engineLog) do
        if entry.sequence>(a.after or 0) and #out<(a.limit or 50) then out[#out+1]=entry end
    end
    return {items=out,next_cursor=#out>0 and out[#out].sequence or (a.after or 0),
        oldest_available=#engineLog>0 and engineLog[1].sequence or 0,last_sequence=logSequence,
        gap=(#engineLog>0 and (a.after or 0)<engineLog[1].sequence-1)}
end
