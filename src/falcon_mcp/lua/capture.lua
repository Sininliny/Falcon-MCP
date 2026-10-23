-- Live MIDI capture. Records the events that reach this Master Script Processor (in the standalone: the
-- player's keyboard, pedal and wheels) into memory; Python pages them out and writes .mid/.json files.
-- No file I/O here: onEvent runs in the audio context.
local CAPTURE_LIMIT=50000
local CAPTURE_PAGE=500
local capture={armed=false,events={},count=0,dropped=0,generation=0}
local captureCallbacks={[144]="onNote",[128]="onRelease",[176]="onController",
    [224]="onPitchBend",[208]="onAfterTouch",[160]="onPolyAfterTouch",[192]="onProgramChange"}

local function captureNow()
    local ok,now=pcall(getTime)
    if ok and type(now)=="number" then return now end
    return nil
end

local function captureExpire(now)
    if capture.armed and now and capture.deadline and now>capture.deadline then
        capture.armed=false;capture.stopped=now;capture.expired=true
    end
end

local function captureRecord(event)
    local now=captureNow()
    captureExpire(now)
    if not capture.armed or not now then return end
    local kind=event.type
    local a,b
    if kind==144 or kind==128 then a=event.note;b=event.velocity
    elseif kind==176 then a=event.controller;b=event.value
    elseif kind==224 then a=0;b=event.bend or event.value
    elseif kind==208 then a=event.value;b=0
    elseif kind==160 then a=event.note;b=event.value end
    if a==nil then return end
    if capture.count>=CAPTURE_LIMIT then capture.dropped=capture.dropped+1;return end
    capture.count=capture.count+1
    capture.events[capture.count]={now-capture.started,kind,event.channel or 1,a,b or 0}
end

-- Overrides the bootstrap's per-type dispatch, so delegate to the same callbacks it would have used
-- (an extension's onNote etc. live in this runtime's environment) and keep default forwarding.
local function captureOnEvent(event)
    local ok,err=pcall(captureRecord,event)
    if not ok then capture.error=tostring(err) end
    local name=captureCallbacks[event.type]
    local callback=name and rawget(_G,name)
    if type(callback)=="function" then return callback(event) end
    return postEvent(event)
end
onEvent=captureOnEvent

local function captureStatus()
    local now=captureNow()
    captureExpire(now)
    return {armed=capture.armed,count=capture.count,dropped=capture.dropped,limit=CAPTURE_LIMIT,
        generation=capture.generation,elapsed_ms=capture.started and ((capture.stopped or now or 0)-capture.started) or 0,
        expired=capture.expired==true,error=capture.error,
        hooked=rawget(_G,"onEvent")==captureOnEvent}
end

function handlers.midi_capture(a)
    if a.action=="start" then
        check(rawget(_G,"onEvent")==captureOnEvent,
            "A native extension defines onEvent; live capture needs the bridge's own onEvent")
        local now=captureNow()
        check(now~=nil,"Falcon time is unavailable; is the audio engine running?")
        local seconds=tonumber(a.max_seconds) or 600
        check(seconds>=1 and seconds<=3600,"max_seconds must be 1..3600")
        capture={armed=true,events={},count=0,dropped=0,generation=capture.generation+1,
            started=now,deadline=now+seconds*1000}
    elseif a.action=="stop" then
        if capture.armed then capture.armed=false;capture.stopped=captureNow() end
    elseif a.action=="read" then
        check(a.generation==capture.generation,"Capture generation changed; a new capture was started")
        local offset,limit=a.offset or 0,math.min(a.limit or CAPTURE_PAGE,CAPTURE_PAGE)
        check(offset>=0 and limit>=1,"Invalid capture page")
        local out={}
        for i=offset+1,math.min(capture.count,offset+limit) do out[#out+1]=capture.events[i] end
        local status=captureStatus()
        status.events=out;status.offset=offset;status.next_offset=offset+#out
        status.has_more=offset+#out<capture.count
        return status
    else
        check(a.action=="status","Unknown capture action")
    end
    return captureStatus()
end

-- A reload would discard the in-memory capture; hold it while recording (until the deadline).
local captureCanReload=falcon_can_reload
function falcon_can_reload()
    captureExpire(captureNow())
    if capture.armed then return false end
    return captureCanReload()
end
