"""Exercise the persistent Lua loader, non-RT compilation, and actual callback dispatch."""

from importlib.resources import files

from lupa.lua51 import LuaRuntime


def loader():
    lua = LuaRuntime(unpack_returned_tuples=True)
    lua.execute("""
      BRIDGE_ROOT="/bridge"; nonRT=false; started={};stopped={};notes={};forwarded={};tasks={};cleaned={};loads=0
      package.preload["uvi.json"]=function() return {decode=function(text) return manifest end} end
      function loadTextData(path)
        if path:match("bootstrap%-epoch.json$") then return {finished=true} end
        if path:match("deployment.json$") then return {finished=true,data="manifest"} end
        return {finished=true,data=runtimeSource}
      end
      function saveData(value,path,cb) lastStatus=value;if cb then cb({success=true}) end end
      function loadState(path,cb)
        loads=loads+1;nonRT=true
        onLoad({_falconBootstrapControl=1})
        nonRT=false;cb({success=true})
        return {finished=true,success=true}
      end
      function spawn(fn) assert(not nonRT); tasks[#tasks+1]=coroutine.create(fn) end
      function wait(ms) assert(not nonRT);coroutine.yield(ms) end
      function postEvent(e) forwarded[#forwarded+1]=e end
      function resumeTask(co) local ok,err=coroutine.resume(co);assert(ok,err) end
    """)
    lua.execute(files("falcon_mcp").joinpath("lua/bootstrap.lua").read_text(encoding="utf8"))
    lua.execute("controller=coroutine.create(onInit)")
    return lua


def publish(lua, letter, *, invalid=False, callbacks=True):
    build = letter * 64
    source = f'local BUILD_ID = "{build}"\n'
    if invalid:
        source += "this is invalid lua syntax !"
    else:
        source += """
          assert(nonRT,"compilation must not execute on audio callback")
          function falcon_can_reload() return not busy end
          function onInit()
            started[#started+1]=BUILD_ID
            while FALCON_BOOTSTRAP.active do wait(100) end
            stopped[#stopped+1]=BUILD_ID
          end
          function onUnload() cleaned[#cleaned+1]=BUILD_ID end
        """
        if callbacks:
            source += """
              function onNote(e) notes[#notes+1]=e end
              function onSave() return {value=BUILD_ID} end
              function onLoad(data) restored=data end
            """
    lua.globals().runtimeSource = source
    lua.globals().manifest = lua.table_from({"build_id": build})
    return build


def test_loader_compiles_off_audio_thread_swaps_generation_and_forwards_midi():
    lua = loader()
    a = publish(lua, "a")
    lua.execute("resumeTask(controller);resumeTask(tasks[1])")
    assert lua.globals().started[1] == a
    assert lua.globals().onSave().value == a
    lua.execute("onEvent({type=144,note=60});onEvent({type=128,note=60})")
    assert len(lua.globals().notes) == 1 and len(lua.globals().forwarded) == 1
    b = publish(lua, "b", callbacks=False)
    lua.execute("resumeTask(controller);resumeTask(tasks[1]);resumeTask(tasks[2])")
    assert lua.globals().stopped[1] == a and lua.globals().started[2] == b
    assert lua.globals().cleaned[1] == a
    assert len(lua.globals().onSave()) == 0  # absent user callback must not recurse into bootstrap
    lua.execute("onLoad({custom=1});onEvent({type=144,note=61})")
    assert len(lua.globals().forwarded) == 2


def test_bad_update_keeps_previous_runtime_and_corrected_source_recovers():
    lua = loader()
    a = publish(lua, "a")
    lua.execute("resumeTask(controller)")
    publish(lua, "b", invalid=True)
    lua.execute("resumeTask(controller)")
    assert lua.globals().onSave().value == a
    assert lua.globals().lastStatus.error
    assert lua.globals().loads == 2
    lua.execute("resumeTask(controller)")
    assert lua.globals().loads == 2  # no unbounded retries of a broken revision
    c = publish(lua, "c")
    lua.execute("resumeTask(controller)")
    assert lua.globals().onSave().value == c and lua.globals().lastStatus.error == ""


def test_busy_runtime_defers_replacement_until_idle():
    lua = loader()
    a = publish(lua, "a")
    lua.execute("resumeTask(controller);busy=true")
    b = publish(lua, "b")
    lua.execute("resumeTask(controller)")
    assert lua.globals().onSave().value == a and lua.globals().loads == 1
    lua.execute("busy=false;resumeTask(controller)")
    assert lua.globals().onSave().value == b


def test_loaded_source_must_match_published_build_id():
    lua = loader()
    publish(lua, "a")
    lua.globals().manifest = lua.table_from({"build_id": "b" * 64})
    lua.execute("resumeTask(controller)")
    assert lua.globals().loads == 0
    assert "mismatched" in lua.globals().lastStatus.error
