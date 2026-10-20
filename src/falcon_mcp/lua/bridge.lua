-- UVI Falcon 2027 / UVIScript Lua 5.1. BRIDGE_ROOT and TOKEN are generated.
-- Use documented asynchronous file I/O only. Never load/execute mailbox code.
local json = require("uvi.json")
local lastId = ""
local initialized = false
local lastMutation = {}
local voices = {}
local pending = nil
local playing = false
local sequenceGeneration = 0
local sequenceError = ""
local bridgeInstance=tostring({})
local collections = {parts=true, layers=true, keygroups=true, oscillators=true,
    inserts=true, auxs=true, sends=true, mods=true, eventProcessors=true, children=true}
local mutations = {set_parameters=true, edit_effect=true, load_sample=true,
    playback=true, purge=true, route=true, midi=true, play=true, panic=true,
    set_connection=true}

local function check(condition, message)
    if not condition then error(message) end
end

local function safeProperty(node, key)
    local ok, value = pcall(function() return node[key] end)
    if ok then return value end
    return nil
end

local function collection(node, field)
    local list = safeProperty(node, field)
    if field ~= "children" or not list then return list end
    local out = {}
    for _, child in pairs(list) do out[#out+1]=child end
    table.sort(out,function(a,b) return a.name < b.name end)
    return out
end

-- A restricted selector grammar, not Lua evaluation. Indices are 1-based.
-- Examples: Synth.parts[1].program.layers[1].keygroups[1].oscillators[1]
local function resolve(selector)
    check(type(selector) == "string" and #selector <= 512, "Invalid selector")
    local root, rest = string.match(selector, "^(%a+)(.*)$")
    local node = nil
    if root == "Synth" then node = Synth
    elseif root == "Part" then node = Part
    elseif root == "Program" then node = Program
    elseif root == "Layer" then node = Layer end
    check(node ~= nil, "Root unavailable; use Synth.parts[1].program for a Master script")
    while rest ~= "" do
        local field, index, tail = string.match(rest, "^%.(%a+)%[(%d+)%](.*)$")
        if field then
            check(collections[field], "Unsupported indexed collection")
            local list = collection(node, field)
            node = list and list[tonumber(index)]
            rest = tail
        else
            field, tail = string.match(rest, "^%.(program)(.*)$")
            check(field ~= nil, "Selector must use .collection[1] or .program")
            node = safeProperty(node, "program")
            rest = tail
        end
        check(node ~= nil, "Selector does not exist; re-query the tree")
    end
    return node
end

local function summary(node, selector)
    return {selector=selector, name=safeProperty(node,"name"),
        displayName=safeProperty(node,"displayName"), type=safeProperty(node,"type"),
        path=safeProperty(node,"path"), numParams=safeProperty(node,"numParams")}
end

local function definitions(node)
    local out = {}
    for _, d in pairs(node.parameterDefinitions) do out[#out+1] = d end
    table.sort(out, function(a,b) return a.name < b.name end)
    return out
end

local function definition(node, name)
    check(node:hasParameter(name), "Unknown parameter: " .. tostring(name))
    for _, d in pairs(node.parameterDefinitions) do
        if d.name == name then return d end
    end
    error("Parameter definition unavailable: " .. tostring(name))
end

local function validateValue(d, value)
    check(not d.readOnly, "Read-only parameter: " .. d.name)
    local t = d.type
    if t == "int" or t == "float" then
        check(type(value) == "number" and value == value and math.abs(value) < math.huge,
            "Expected finite number for " .. d.name)
        if t == "int" then check(value == math.floor(value), "Expected integer for " .. d.name) end
        -- UVI uses min > max for an UNBOUNDED numeric parameter (e.g. Ratio).
        local unbounded = d.min ~= nil and d.max ~= nil and d.min > d.max
        if not unbounded then
            if d.min ~= nil then check(value >= d.min, "Below minimum for " .. d.name) end
            if d.max ~= nil then check(value <= d.max, "Above maximum for " .. d.name) end
        end
    elseif t == "bool" then check(type(value) == "boolean", "Expected boolean for " .. d.name)
    elseif t == "string" then check(type(value) == "string" and #value <= 4096, "Expected string for " .. d.name)
    else error("Unsupported parameter type: " .. tostring(t)) end
end

local function startTask(start, finish)
    -- Do NOT yield across pcall's C boundary in Lua 5.1. Await outside pcall.
    pending = {done=false}
    local current = pending
    local task = start(function(t)
        current.ok = t.success
        if t.success then
            local ok, value = pcall(finish)
            current.ok = ok
            if ok then current.result=value else current.error=tostring(value) end
        else
            current.error="Falcon async operation failed; refresh selector/type/slot"
        end
        current.done = true
    end)
    check(task ~= nil, "Async operation returned no task")
    return {}
end

local handlers = {}
function handlers.ping(a)
    return {protocol=1, bridgeVersion="0.2.0", instance=bridgeInstance, sampleRate=getSamplingRate(),
        tempo=getTempo(), time=getTime(), lastMutation=lastMutation,
        sequence={playing=playing,generation=sequenceGeneration,error=sequenceError},
        scope=summary(this.parent, ""),
        limitations={"No public layer/keygroup/oscillator creation API", "No public Program save/load API"}}
end

function handlers.children(a)
    local node = resolve(a.selector)
    local list, out = collection(node, a.collection), {}
    check(collections[a.collection], "Unsupported collection")
    if not list then return {items=out, total_count=0, has_more=false} end
    local total = #list
    local offset, limit = a.offset or 0, a.limit or 30
    check(offset >= 0 and limit >= 1 and limit <= 100, "Invalid pagination")
    for i=offset+1, math.min(total, offset+limit) do
        out[#out+1] = summary(list[i], a.selector .. "." .. a.collection .. "[" .. i .. "]")
    end
    return {node=summary(node, a.selector), items=out, total_count=total,
        has_more=offset+limit < total, next_offset=math.min(total, offset+limit)}
end

function handlers.parameters(a)
    local node, out = resolve(a.selector), {}
    local defs = definitions(node)
    local filtered = {}
    for _, d in ipairs(defs) do
        if not a.filter or string.find(string.lower(d.name), string.lower(a.filter), 1, true) then
            filtered[#filtered+1] = d
        end
    end
    local offset, limit = a.offset or 0, a.limit or 30
    for i=offset+1, math.min(#filtered,offset+limit) do
        local d = filtered[i]
        out[#out+1] = {definition=d, value=node:getParameter(d.name)}
    end
    return {items=out, total_count=#filtered, has_more=offset+limit < #filtered,
        next_offset=math.min(#filtered,offset+limit)}
end

function handlers.get_parameters(a)
    local node, out = resolve(a.selector), {}
    for _, name in ipairs(a.names) do
        definition(node,name)
        out[#out+1] = {name=name, value=node:getParameter(name)}
    end
    return {items=out}
end

local function setParameters(node,a)
    local out = {}
    if a.expected_path then check(node.path==a.expected_path,"Stale node identity; re-query tree") end
    -- Validate the entire batch and compare expected values BEFORE any mutation.
    for _, change in ipairs(a.changes) do
        validateValue(definition(node,change.name),change.value)
        if change.expected ~= nil then
            check(node:getParameter(change.name) == change.expected,
                "Stale expected value for " .. change.name .. "; refresh before editing")
        end
    end
    for _, change in ipairs(a.changes) do
        local before = node:getParameter(change.name)
        local applied,failure=pcall(function() node:setParameter(change.name,change.value) end)
        if not applied then
            out[#out+1]={name=change.name,before=before,requested=change.value,error=tostring(failure),verified=false}
            return {items=out,verified=false,partial=#out>1,error=tostring(failure)}
        end
        local after = node:getParameter(change.name)
        local matches = after == change.value
        if type(after) == "number" and type(change.value) == "number" then
            matches = math.abs(after-change.value) <= 0.00001*math.max(1,math.abs(change.value))
        end
        out[#out+1] = {name=change.name, before=before, requested=change.value, after=after, verified=matches}
        -- Return the actual outcome, never silently report success on a no-op.
        if not matches then return {items=out, verified=false, partial=true} end
    end
    return {items=out, verified=true, partial=false}
end

function handlers.set_parameters(a)
    return setParameters(resolve(a.selector),a)
end

function handlers.set_connection(a)
    local node=resolve(a.selector)
    definition(node,a.parameter)
    local connections=node:getParameterConnections(a.parameter) or {}
    check(connections[a.index] ~= nil,"Connection no longer exists; refresh connections")
    return setParameters(connections[a.index],a)
end

function handlers.connections(a)
    local node, out = resolve(a.selector), {}
    definition(node,a.parameter)
    for i,c in ipairs(node:getParameterConnections(a.parameter) or {}) do
        local params = {}
        for _,d in ipairs(definitions(c)) do
            params[#params+1] = {definition=d,value=c:getParameter(d.name)}
        end
        out[#out+1] = {index=i,name=c.name,type=c.type,parameters=params}
    end
    return {items=out}
end

function handlers.edit_effect(a)
    local node = resolve(a.selector)
    check(safeProperty(node,"inserts") ~= nil, "Node has no insert chain")
    check(a.action == "insert" or a.action == "remove" or a.action == "move", "Unknown effect edit action")
    return startTask(function(cb)
        if a.action == "insert" then return node:insertEffect(a.type,a.slot,cb)
        elseif a.action == "remove" then return node:removeEffect(a.slot,cb)
        else return node:moveEffect(a.slot,a.to_slot,cb) end
    end, function() return handlers.children({selector=a.selector,collection="inserts",limit=100}) end)
end

function handlers.load_sample(a)
    local node = resolve(a.selector)
    return startTask(function(cb) return loadSample(node,a.path,cb) end,
        function() return {loaded=true,sampleInfo=safeProperty(node,"sampleInfo")} end)
end

function handlers.playback(a)
    local node = resolve(a.selector)
    return startTask(function(cb) return setPlaybackOptions(node,a.start,a.finish,a.loop_type,
        a.loop_start,a.loop_end,a.forward,cb) end, function() return {applied=true} end)
end

function handlers.purge(a)
    local node = resolve(a.selector)
    check(node.type~="SampleMappingOscillator","Use mapping purge/unpurge for MultiSample; generic purge is a silent no-op")
    return startTask(function(cb)
        if a.restore then return unpurge(node,cb) end
        return purge(node,cb)
    end, function() return {applied=true} end)
end

function handlers.route(a)
    local node = resolve(a.selector)
    node.output = a.output
    pending = {done=false}
    local current = pending
    spawn(function()
        wait(50)
        local ok, actual = pcall(function() return node.output end)
        current.ok=ok
        if ok then current.result={requested=a.output,actual=actual,verified=actual == a.output}
        else current.error=tostring(actual) end
        current.done=true
    end)
    return {}
end

function handlers.play(a)
    check(not playing, "A test sequence is already scheduled; wait or call panic")
    playing=true
    sequenceGeneration=sequenceGeneration+1
    local generation=sequenceGeneration
    sequenceError=""
    spawn(function()
        local ownVoices={}
        local elapsed, finish = 0, 0
        for _, n in ipairs(a.notes) do
            if n.delay_ms and n.delay_ms > 0 then
                wait(n.delay_ms)
                elapsed=elapsed+n.delay_ms
            end
            if generation ~= sequenceGeneration then return end
            local ok, id = pcall(playNote,n.note,n.velocity,n.duration_ms,a.layer,a.channel or 1)
            if not ok then
                sequenceError=tostring(id)
                print("MCP playNote failed: " .. sequenceError)
                playing=false
                return
            end
            voices[#voices+1] = id
            ownVoices[id]=true
            finish=math.max(finish,elapsed+n.duration_ms)
        end
        if finish > elapsed then wait(finish-elapsed) end
        if generation == sequenceGeneration then
            local remaining={}
            for _,id in ipairs(voices) do if not ownVoices[id] then remaining[#remaining+1]=id end end
            voices=remaining;playing=false
        end
    end)
    return {scheduled=#a.notes, scope="Downstream of this Script Processor; use a Part/Program bridge for audition"}
end

function handlers.midi(a)
    if a.kind == "cc" then controlChange(a.number,a.value,a.channel or 1)
    elseif a.kind == "pitch_bend" then pitchBend(a.value,a.channel or 1)
    elseif a.kind == "aftertouch" then afterTouch(a.value,a.channel or 1)
    elseif a.kind == "program_change" then programChange(a.value,a.channel or 1)
    else error("Unsupported MIDI message") end
    return {sent=true}
end

function handlers.panic(a)
    playing=false
    sequenceGeneration=sequenceGeneration+1
    for _, id in ipairs(voices) do pcall(function() releaseVoice(id) end) end
    voices = {}
    return {released=true, scope="Voices generated by this bridge only"}
end

local function process(req)
    check(req.protocol == 1 and req.token == TOKEN and type(req.id) == "string", "Invalid request")
    local handler = handlers[req.op]
    check(handler ~= nil, "Unsupported operation")
    return handler(req.args or {})
end

function onInit()
    print("Falcon MCP bridge ready: " .. BRIDGE_ROOT)
    while true do
        local task = loadTextData(BRIDGE_ROOT .. "/request.json")
        while not task.finished do wait(20) end
        if type(task.data) == "string" and #task.data <= 65536 then
            local decoded, req = pcall(json.decode, task.data)
            -- A reload must NEVER replay a previous mutation left on disk.
            if not initialized and decoded and type(req) == "table" then lastId=req.id end
            if decoded and type(req) == "table" and req.token == TOKEN and req.id ~= lastId then
                lastId = req.id
                print("MCP request",req.id,req.op)
                pending=nil
                local ok, result = pcall(process, req)
                if ok and pending then
                    while not pending.done do wait(20) end
                    ok=pending.ok
                    if ok then result=pending.result else result=pending.error end
                end
                local response = {id=req.id, token=TOKEN, ok=ok, protocol=1}
                if ok then response.result=result else response.error=tostring(result) end
                if mutations[req.op] then
                    lastMutation={id=req.id, op=req.op, ok=ok, result=response.result,error=response.error}
                end
                local saved = false
                saveData(response, BRIDGE_ROOT .. "/response.json",function(t)
                    if not t.success then print("MCP response write failed",req.id) end
                    saved=true
                end)
                while not saved do wait(20) end
                print("MCP result",req.id,req.op,ok)
            end
        end
        initialized=true
        wait(100)
    end
end
