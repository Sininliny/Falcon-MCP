-- Offline UVIScript UI double for falcon_ui_check. It runs a script's main chunk in a
-- sandbox, records the widget tree, and logs engine writes made by widget callbacks.
-- This is a model of the documented UVIScript UI API, not Falcon itself: real
-- rendering, hit-testing and mouse input are never exercised here.
-- The host (Python) sets ENGINE (element description tree) and LIMIT before running.

local M = {}
local log = {}           -- engine writes, ordered
local accesses = {}      -- unmodelled API used by the script
local widgets = {}       -- creation order
local root = {kind="Root", children={}, props={}}
local ui = {performanceView=false, width=nil, height=nil, background=nil, backgroundColour=nil}
local current = nil      -- widget whose callback is running
local recording = true   -- false while the host restores values

local function note(kind, name)
    local key = kind .. ":" .. tostring(name)
    if not accesses[key] then accesses[key] = {kind=kind, name=tostring(name), count=0} end
    accesses[key].count = accesses[key].count + 1
end

-- A permissive stand-in for unmodelled globals and methods: indexable, callable, comparable.
local Stub
Stub = setmetatable({}, {
    __index=function(t, k) note("index", k); return Stub end,
    __newindex=function() end,
    __call=function() return Stub end,
    __tostring=function() return "<stub>" end,
    __len=function() return 0 end,
    __concat=function(a, b) return tostring(a) .. tostring(b) end,
})

---------------------------------------------------------------------------- engine

local Element = {}
local function makeElement(desc, parent)
    local e = {desc=desc, parent=parent, values={}}
    for k, v in pairs(desc.values or {}) do e.values[k] = v end
    return setmetatable(e, Element)
end

local collectionNames = {layers=true, keygroups=true, oscillators=true, inserts=true, auxs=true,
    eventProcessors=true, modulations=true, parts=true, children=true}

local methods = {}
function methods:getParameter(name)
    local v = self.values[name]
    if v == nil and not self.desc.placeholder then note("unknown_parameter", self.desc.selector .. ":" .. tostring(name)) end
    return v
end
function methods:hasParameter(name) return self.values[name] ~= nil end
function methods:setParameter(name, value)
    local before = self.values[name]
    self.values[name] = value
    if recording then
        log[#log+1] = {selector=self.desc.selector, native=self.desc.native, type=self.desc.type,
            parameter=tostring(name), value=value, before=before,
            known=(before ~= nil), placeholder=self.desc.placeholder or false, widget=current and current.props.name or nil}
    end
end
function methods:getParameterConnections() return {} end
function methods:getParameterNormalized(name) return self:getParameter(name) end
function methods:setParameterNormalized(name, value) return self:setParameter(name, value) end

Element.__index = function(e, k)
    if methods[k] then return methods[k] end
    local d = e.desc
    if k == "name" then return d.name end
    if k == "displayName" then return d.displayName or d.name end
    if k == "type" then return d.type end
    if k == "path" then return d.selector end
    if k == "parent" then return e.parent end
    if k == "parameterDefinitions" then return d.definitions or {} end
    if k == "numParams" then return #(d.definitions or {}) end
    if k == "program" then
        if d.program then
            if not rawget(e, "programElement") then rawset(e, "programElement", makeElement(d.program, e)) end
            return rawget(e, "programElement")
        end
        return nil
    end
    if collectionNames[k] then
        local lists = rawget(e, "lists")
        if not lists then lists = {}; rawset(e, "lists", lists) end
        if not lists[k] then
            local list = {}
            for i, child in ipairs((d.collections or {})[k] or {}) do list[i] = makeElement(child, e) end
            -- Name lookup is common in scripts (Program.layers["Body"]). Without a preset (LENIENT),
            -- missing items become placeholders so the UI can still be built and checked.
            setmetatable(list, {__index=function(t, key)
                if type(key) == "string" then
                    for _, item in ipairs(t) do
                        if item.desc.name == key or item.desc.displayName == key then return item end
                    end
                end
                if M.lenient and (type(key) == "string" or type(key) == "number") then
                    local suffix = type(key) == "number" and ("[" .. key .. "]") or ("[\"" .. key .. "\"]")
                    local item = makeElement({type="Unknown", name=tostring(key), placeholder=true,
                        selector=tostring(d.selector) .. "." .. k .. suffix, values={}}, e)
                    rawset(t, key, item)
                    return item
                end
                return nil
            end})
            lists[k] = list
        end
        return lists[k]
    end
    note("element_member", tostring(d.type) .. "." .. tostring(k))
    return nil
end

---------------------------------------------------------------------------- widgets

local Widget = {}
local valueKinds = {Knob=true, Slider=true, NumBox=true, OnOffButton=true, Menu=true,
    MultiStateButton=true, XY=true, Table=true, ParamKnob=true, ParamSlider=true,
    ParamOnOffButton=true, ParamMenu=true, ParamNumBox=true}

local function callChanged(w)
    local f = w.props.changed
    if type(f) ~= "function" then return end
    local previous = current
    current = w
    local ok, err = pcall(f, w)
    current = previous
    if not ok then
        w.errors[#w.errors+1] = tostring(err)
    end
end

local function setValue(w, v, notify)
    w.props.value = v
    w.valueSet = true
    if notify ~= false then callChanged(w) end
end

local widgetMethods = {}
function widgetMethods:setValue(v, notify) setValue(self, v, notify) end
function widgetMethods:setRange(lo, hi) self.props.min = lo; self.props.max = hi end
function widgetMethods:push(notify) if notify ~= false then callChanged(self) end end
function widgetMethods:setStripImage(path, frames) self.props.stripImage = path end
function widgetMethods:setLottieAnimation(path) self.props.lottieAnimation = path end
function widgetMethods:bind(target, param)
    self.binding = {target=type(target) == "table" and getmetatable(target) == Element and target.desc.selector
        or tostring(target), parameter=param}
end
function widgetMethods:getValue() return self.props.value end
function widgetMethods:setItems(items) self.props.items = items end

local function setBounds(w, b)
    w.props.x, w.props.y, w.props.width, w.props.height = b[1], b[2], b[3], b[4]
    w.explicit.x, w.explicit.y, w.explicit.width, w.explicit.height = true, true, true, true
end

Widget.__index = function(w, k)
    if widgetMethods[k] then return widgetMethods[k] end
    if M.widgetFactories[k] and (w.kind == "Panel" or w.kind == "Viewport") then
        return function(self, ...) return M.create(k, self, ...) end
    end
    local p = w.props
    if k == "w" then return p.width elseif k == "h" then return p.height end
    if k == "bounds" then return {p.x, p.y, p.width, p.height} end
    if k == "position" or k == "pos" then return {p.x, p.y} end
    if k == "size" then return {p.width, p.height} end
    if k == "visible" or k == "enabled" or k == "interceptsMouseClicks" or k == "persistent" then
        if p[k] == nil then return k ~= "persistent" or valueKinds[w.kind] or false end
    end
    return p[k]
end

local tracked = {x=true, y=true, width=true, height=true, visible=true, enabled=true,
    interceptsMouseClicks=true, alpha=true, displayName=true, text=true}

Widget.__newindex = function(w, k, v)
    local p = w.props
    if k == "w" then k = "width" elseif k == "h" then k = "height" end
    if k == "bounds" then setBounds(w, v); return end
    if k == "position" or k == "pos" then
        p.x, p.y = v[1], v[2]; w.explicit.x, w.explicit.y = true, true; return
    end
    if k == "size" then
        p.width, p.height = v[1], v[2]; w.explicit.width, w.explicit.height = true, true; return
    end
    if k == "name" then error("Widget name is read-only") end
    if k == "x" or k == "y" or k == "width" or k == "height" then w.explicit[k] = true end
    if current and current ~= w and tracked[k] and recording then
        log[#log+1] = {ui=true, target=p.name, property=k, value=v, widget=current.props.name}
    end
    if k == "value" then setValue(w, v, true); return end
    p[k] = v
end

local defaultSizes = {
    Knob={100, 50}, Slider={100, 50}, ParamKnob={100, 50}, ParamSlider={100, 50},
    NumBox={100, 20}, ParamNumBox={100, 20}, Button={100, 20}, OnOffButton={100, 20},
    ParamOnOffButton={100, 20}, Menu={100, 40}, ParamMenu={100, 40}, MultiStateButton={100, 20},
    Label={100, 20}, Image={100, 100}, SVG={100, 100}, Panel={710, 100}, Viewport={710, 100},
    Table={220, 100}, XY={100, 100}, AudioMeter={100, 20}, WaveView={220, 100},
    FileSelector={220, 20}, DnDArea={100, 100},
}

-- Constructor positional arguments after the name, per lua.uvi.net class references.
local positional = {
    Knob={"value", "min", "max", "integer"}, Slider={"value", "min", "max", "integer"},
    NumBox={"value", "min", "max", "integer"}, OnOffButton={"value"},
    Menu={"items", "value"}, MultiStateButton={"items", "value"},
    Table={"size", "value", "min", "max", "integer"}, XY={"paramX", "paramY"},
    ParamKnob={"element", "parameter"}, ParamSlider={"element", "parameter"},
    ParamOnOffButton={"element", "parameter"}, ParamMenu={"element", "parameter"},
    ParamNumBox={"element", "parameter"}, Image={"path"}, SVG={"path"},
}

function M.create(kind, parent, ...)
    local args = {...}
    local spec = args[1]
    local named = {}
    local list = {}
    if type(spec) == "table" and select("#", ...) == 1 then
        for k, v in pairs(spec) do if type(k) ~= "number" then named[k] = v end end
        for i = 1, #spec do list[i] = spec[i] end
    else
        list = args
    end
    local w = setmetatable({kind=kind, props={}, explicit={}, children={}, errors={},
        parent=parent, index=#widgets + 1, line=nil}, Widget)
    local p = w.props
    p.name = list[1] ~= nil and tostring(list[1]) or (kind .. tostring(#widgets + 1))
    for i, key in ipairs(positional[kind] or {}) do
        if list[i + 1] ~= nil then p[key] = list[i + 1] end
    end
    if kind == "Menu" or kind == "MultiStateButton" then
        if p.value == nil then p.value = 1 end
    elseif kind == "OnOffButton" then
        if p.value == nil then p.value = false end
    elseif kind == "Knob" or kind == "Slider" or kind == "NumBox" then
        if p.value == nil then p.value = 0 end
        if p.min == nil then p.min = 0 end
        if p.max == nil then p.max = 1 end
    end
    p.default = p.value
    if p.element ~= nil then
        w.binding = {target=type(p.element) == "table" and getmetatable(p.element) == Element
            and p.element.desc.selector or tostring(p.element), parameter=p.parameter,
            native=type(p.element) == "table" and getmetatable(p.element) == Element and p.element.desc.native or nil}
        p.element = nil
        if p.parameter then p.parameter = tostring(p.parameter) end
    end
    local size = defaultSizes[kind] or {100, 20}
    p.width, p.height = size[1], size[2]
    local info = debug.getinfo(3, "l")
    w.line = info and info.currentline or nil
    widgets[#widgets+1] = w
    if parent.kind == "Viewport" then parent.children = {} end
    parent.children[#parent.children+1] = w
    -- Named fields run through the same setters as script assignments.
    local changed = named.changed
    named.changed = nil
    for k, v in pairs(named) do Widget.__newindex(w, k, v) end
    if changed then p.changed = changed end
    if kind == "Menu" or kind == "MultiStateButton" then
        p.items = p.items or {}
    end
    return w
end

M.widgetFactories = {}
for kind in pairs(defaultSizes) do
    M.widgetFactories[kind] = true
end

---------------------------------------------------------------------------- environment

local function ParameterValue(...)
    local w = M.create("ParameterValue", {kind="Hidden", children={}}, ...)
    w.hidden = true
    return w
end

function M.environment(engine, scope)
    local env = {}
    local safe = {"assert", "error", "ipairs", "next", "pairs", "pcall", "select", "tonumber", "tostring",
        "type", "unpack", "xpcall", "rawget", "rawset", "rawequal", "setmetatable", "getmetatable"}
    for _, k in ipairs(safe) do env[k] = _G[k] end
    env.string, env.table, env.math = string, table, math
    env.os = {time=os.time, clock=os.clock, date=os.date}
    env.print = function(...) end
    local synth = makeElement(engine, nil)
    env.Synth = synth
    local program = nil
    if scope.part then
        local part = synth.parts[scope.part]
        env.Part = part
        program = part and part.program
    end
    env.Program = program
    local container = program
    if program and scope.layer then
        env.Layer = program.layers[scope.layer]
        container = env.Layer
    end
    env.this = {parent=container, program=program}
    for kind in pairs(M.widgetFactories) do
        env[kind] = function(...) return M.create(kind, root, ...) end
    end
    env.ParameterValue = ParameterValue
    env.makePerformanceView = function() ui.performanceView = true end
    env.setSize = function(w, h) ui.width, ui.height = w, h end
    env.setHeight = function(h) ui.height = h end
    env.setBackground = function(path) ui.background = path end
    env.setBackgroundColour = function(colour) ui.backgroundColour = colour end
    env.moveControl = function(w, col, row)
        w.props.x, w.props.y = 5 + col * 120, 5 + row * 25
        w.explicit.x, w.explicit.y = true, true
    end
    env.setKeyColour = function() end
    env.resetKeyColour = function() end
    env.getSamplingRate = function() return 48000 end
    env.getTempo = function() return 120 end
    env.getTime = function() return 0 end
    env.wait = function() end
    env.waitBeat = function() end
    env.spawn = function() note("deferred", "spawn") end
    env.run = function() note("deferred", "run") end
    env.Mapper = setmetatable({}, {__index=function(_, k) return tostring(k) end})
    env.Unit = setmetatable({}, {__index=function(_, k) return tostring(k) end})
    env.RectanglePlacement = env.Unit
    env._G = env
    setmetatable(env, {__index=function(_, k)
        note("global", k)
        return Stub
    end})
    return env
end

---------------------------------------------------------------------------- driver

local function guard(limit)
    local count = 0
    debug.sethook(function()
        count = count + 1
        if count > limit then
            debug.sethook()
            error("instruction budget exceeded (possible infinite loop or recursion)")
        end
    end, "", 1000)
end

function M.run(source, engine, scope, budget)
    local chunk, err = loadstring(source, "=script")
    if not chunk then return {compiled=false, error=tostring(err)} end
    local env = M.environment(engine, scope)
    setfenv(chunk, env)
    guard(budget)
    local ok, runError = pcall(chunk)
    local initError = nil
    if ok and type(rawget(env, "onInit")) == "function" then
        local initOk, e = pcall(rawget(env, "onInit"))
        if not initOk then initError = tostring(e) end
    end
    debug.sethook()
    M.env = env
    return {compiled=true, ok=ok, error=ok and nil or tostring(runError), init_error=initError,
        callbacks={onNote=type(rawget(env, "onNote")) == "function", onSave=type(rawget(env, "onSave")) == "function",
            onLoad=type(rawget(env, "onLoad")) == "function", onInit=type(rawget(env, "onInit")) == "function"}}
end

function M.widgets() return widgets end
function M.ui() return ui end
function M.accesses()
    local out = {}
    for _, v in pairs(accesses) do out[#out+1] = v end
    return out
end
function M.takeLog()
    local out = log
    log = {}
    return out
end

-- Drive one widget like a value change from the UI, with a budget; returns the log.
function M.drive(w, action, value, budget)
    log = {}
    local before = #w.errors
    guard(budget)
    if action == "push" then
        callChanged(w)
    else
        setValue(w, value, true)
    end
    debug.sethook()
    local errors = {}
    for i = before + 1, #w.errors do errors[#errors+1] = w.errors[i] end
    return M.takeLog(), errors
end

function M.restore(w, value, budget)
    recording = false
    guard(budget)
    pcall(setValue, w, value, true)
    debug.sethook()
    recording = true
    log = {}
end

function M.snapshot()
    local out = {}
    for i, w in ipairs(widgets) do
        out[i] = {visible=w.props.visible, enabled=w.props.enabled,
            interceptsMouseClicks=w.props.interceptsMouseClicks, x=w.props.x, y=w.props.y}
    end
    return out
end

return M
