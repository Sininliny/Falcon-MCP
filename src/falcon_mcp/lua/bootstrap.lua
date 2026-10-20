-- Persistent loader. BRIDGE_ROOT is generated; no source is accepted from the mailbox.
-- Compilation occurs ONLY in onLoad (a documented non-real-time callback).
local json=require("uvi.json")
local current=nil
local staged=nil
local compiled=nil
local attempted=nil
local failure=""
local shared={}
local epoch=0
local generation=0
local function report()
    saveData({version=1,activeBuild=current and current.build or "",attemptedBuild=attempted or "",
        error=failure},BRIDGE_ROOT.."/bootstrap-status.json")
end

function onLoad(data)
    if type(data)=="table" and data._falconBootstrapControl==1 and staged then
        compiled=nil
        local chunk,message=loadstring(staged.source,"@falcon_runtime.lua")
        if not chunk then failure=tostring(message);return end
        generation=generation+1
        local context={active=true,version=1,automaticReload=true,shared=shared,error="",
            instanceNonce=tostring(epoch)..":"..tostring(generation)}
        local env={FALCON_BOOTSTRAP=context}
        env._G=env
        setmetatable(env,{__index=_G})
        setfenv(chunk,env)
        local ok,err=pcall(chunk)
        if not ok then failure=tostring(err);return end
        compiled={environment=env,context=context,build=staged.build}
    elseif current and type(rawget(current.environment,"onLoad"))=="function" then
        return rawget(current.environment,"onLoad")(data)
    end
end

function onSave()
    if current and type(rawget(current.environment,"onSave"))=="function" then
        return rawget(current.environment,"onSave")()
    end
    return {}
end

-- onEvent overrides the individual MIDI callbacks in UVIScript. Delegate with
-- the same priority and preserve default MIDI forwarding when no handler exists.
local midiCallbacks={[144]="onNote",[128]="onRelease",[176]="onController",
    [224]="onPitchBend",[208]="onAfterTouch",[160]="onPolyAfterTouch",[192]="onProgramChange"}
function onEvent(event)
    local env=current and current.environment
    local callback=env and (rawget(env,"onEvent") or rawget(env,midiCallbacks[event.type] or ""))
    if type(callback)=="function" then return callback(event) end
    return postEvent(event)
end
function onTransport(playing)
    local callback=current and rawget(current.environment,"onTransport")
    if type(callback)=="function" then return callback(playing) end
end

function onInit()
    -- Persistent monotonic epoch prevents handle aliases even if the first
    -- mailbox ping left on disk is identical across process restarts.
    local epochTask=loadTextData(BRIDGE_ROOT.."/bootstrap-epoch.json")
    while not epochTask.finished do wait(20) end
    if type(epochTask.data)=="string" then
        local ok,value=pcall(json.decode,epochTask.data)
        if ok and type(value)=="table" and type(value.epoch)=="number" and
            value.epoch>=0 and value.epoch<9007199254740990 then epoch=math.floor(value.epoch) end
    end
    epoch=epoch+1
    local saved=false
    saveData({epoch=epoch},BRIDGE_ROOT.."/bootstrap-epoch.json",function(t)
        saved=true
        if not t.success then failure="Could not persist bootstrap epoch" end
    end)
    while not saved do wait(20) end
    if failure~="" then print(failure);report();return end
    while true do
        local task=loadTextData(BRIDGE_ROOT.."/deployment.json")
        while not task.finished do wait(20) end
        local decoded,manifest=false,nil
        if type(task.data)=="string" and #task.data<16384 then decoded,manifest=pcall(json.decode,task.data) end
        if decoded and type(manifest)=="table" and type(manifest.build_id)=="string" and
            #manifest.build_id==64 and manifest.build_id:match("^%x+$") and manifest.build_id~=attempted then
            local idle=not current or current.environment.falcon_can_reload()
            if idle then
                attempted=manifest.build_id
                local sourceTask=loadTextData(BRIDGE_ROOT.."/falcon_runtime.lua")
                while not sourceTask.finished do wait(20) end
                local source=sourceTask.data
                if type(source)=="string" and #source<=262144 and
                    source:match('local BUILD_ID = "(%x+)"')==attempted then
                    staged={source=source,build=attempted};compiled=nil;failure=""
                    local finished=false
                    local stateTask=loadState(BRIDGE_ROOT.."/bootstrap-trigger.uvs",function(t)
                        if not t.success then failure="Bootstrap state callback failed" end
                        finished=true
                    end)
                    while not finished and not stateTask.finished do wait(20) end
                    if compiled then
                        if current then
                            current.context.active=false
                            local cleanup=rawget(current.environment,"onUnload")
                            if type(cleanup)=="function" then
                                local ok,err=pcall(cleanup)
                                if not ok then print("Falcon extension cleanup failed",tostring(err)) end
                            end
                        end
                        current=compiled;staged=nil;compiled=nil
                        spawn(current.environment.onInit)
                    elseif failure=="" then failure="onLoad did not initialize the runtime" end
                else failure="Missing, oversized, or mismatched runtime source" end
                if current then current.context.error=failure end
                if failure~="" then print("Falcon bootstrap error",failure) end
                report()
            end
        end
        wait(250)
    end
end
