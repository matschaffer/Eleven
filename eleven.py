import re, os, socket, string, subprocess, thread, threading, time
import sublime, sublime_plugin
from functools import partial

max_cols = 60
repls_file = ".eleven.json"

def clean(str):
    return str.translate(None, '\r') if str else None

def symbol_char(char):
    return re.match("[-\w*+!?/.<>]", char)

def project_path(dir_name):
    if os.path.isfile(os.path.join(dir_name, 'project.clj')):
        return dir_name
    else:
        return project_path(os.path.split(dir_name)[0])

def classpath_relative_path(file_name):
    (abs_path, ext) = os.path.splitext(file_name)
    segments = []
    while 1:
        (abs_path, segment) = os.path.split(abs_path)
        if segment == "src": return string.join(segments, "/")
        segments.insert(0, segment)

def output_to_view(v, text):
    v.set_read_only(False)
    edit = v.begin_edit()
    v.insert(edit, v.size(), text)
    v.end_edit(edit)
    v.set_read_only(True)

def exit_with_status(message):
    sublime.status_message(message)
    sys.exit(1)

def exit_with_error(message):
    sublime.error_message(message)
    sys.exit(1)


def get_repl_servers():
    return sublime.load_settings(repls_file).get('repl_servers') or {}

def set_repl_servers(repl_servers, save=True):
    sublime.load_settings(repls_file).set('repl_servers', repl_servers)
    if save:
        sublime.save_settings(repls_file)


def start_repl_server(window_id, cwd):
    proc = subprocess.Popen(["lein", "repl"], stdout=subprocess.PIPE,
                                              stderr=subprocess.PIPE,
                                              cwd=cwd)
    stdout, stderr = proc.communicate()
    match = re.search(r"listening on localhost port (\d+)", stdout)
    if match:
        port = int(match.group(1))
        sublime.set_timeout(partial(on_repl_server_started, window_id, port), 0)
    else:
        sublime.error_message("Unable to start a REPL with `lein repl`")

def on_repl_server_started(window_id, port):
    repl_servers = get_repl_servers()
    repl_servers[window_id] = port
    set_repl_servers(repl_servers)
    sublime.status_message("Clojure REPL started on port " + str(port))


# persistent REPL clients for each window that keep their dynamic vars
clients = {}

class ReplClient:
    def __init__(self, port):
        self.ns = None
        self.view = None

        self.sock = socket.socket()
        self.sock.connect(('localhost', port))
        self.sock.settimeout(10)

    def evaluate(self, exprs, on_complete=None):
        if not self.ns:
            _, self.ns = self._communicate()

        results = []
        for expr in exprs:
            output, next_ns = self._communicate(expr)
            results.append({'ns': self.ns, 'expr': expr, 'output': output})
            self.ns = next_ns

        if on_complete:
            sublime.set_timeout(partial(on_complete, results), 0)
        else:
            return results

    def kill(self):
        try:
            self.evaluate(["(System/exit 0)"])
        except socket.error:
            # Probably already dead
            pass

    def _communicate(self, expr=None):
        if expr:
            self.sock.send(expr + "\n")
        output = ""
        while 1:
            output += self.sock.recv(1024)
            match = re.match(r"(.*\n)?(\S+)=> $", output, re.DOTALL)
            if match:
                return (clean(match.group(1)), match.group(2))
            elif output == "":
                return (None, None)

class LazyViewString:
    def __init__(self, view):
        self.view = view

    def __str__(self):
        if not hasattr(self, '_string_value'):
            self._string_value = self.get_string()
        return self._string_value

class Selection(LazyViewString):
    def get_string(self):
        sel = self.view.sel()
        if len(sel) == 1:
            return self.view.substr(self.view.sel()[0]).strip()
        else:
            exit_with_status("There must be one selection to evaluate")

class SymbolUnderCursor(LazyViewString):
    def get_string(self):
        begin = end = self.view.sel()[0].begin()
        while symbol_char(self.view.substr(begin - 1)): begin -= 1
        while symbol_char(self.view.substr(end)): end += 1
        if begin == end:
            exit_with_status("No symbol found under cursor")
        else:
            return self.view.substr(sublime.Region(begin, end))


class ClojureStartRepl(sublime_plugin.WindowCommand):
    def run(self):
        wid = self.window.id()
        repl_servers = get_repl_servers()
        port = repl_servers.get(str(wid))
        if port:
            client = clients.get(wid)
            if client:
                return
            try:
                clients[wid] = ReplClient(port)
                return
            except socket.error:
                del repl_servers[str(wid)]
                set_repl_servers(repl_servers)

        sublime.status_message("Starting Clojure REPL")
        #FIXME don't use active view
        file_name = self.window.active_view().file_name()
        cwd = None
        if file_name:
            dir_name = os.path.split(file_name)[0]
            cwd = project_path(dir_name) or dir_name
        else:
            for folder in self.window.folders():
                cwd = project_path(folder)
                if cwd: break

        thread.start_new_thread(start_repl_server, (str(wid), cwd))

class ClojureEvaluate(sublime_plugin.TextCommand):
    def run(self, edit, expr, input_panel = None, **kwargs):
        self._window = self.view.window()
        self._expr = expr

        self._window.run_command('clojure_start_repl')

        if input_panel:
            it = input_panel['initial_text']
            on_done = partial(self._handle_input, **kwargs)
            view = self._window.show_input_panel(input_panel['prompt'],
                                                 "".join(it) if it else "",
                                                 on_done, None, None)

            if it and len(it) > 1:
                view.sel().clear()
                offset = 0
                for chunk in it[0:-1]:
                    offset += len(chunk)
                    view.sel().add(sublime.Region(offset))
        else:
            self._handle_input(None, **kwargs)

    def _handle_input(self, from_input_panel, output_to = "repl", **kwargs):
        wid = self._window.id()
        port = get_repl_servers().get(str(wid))
        if not port:
            sublime.set_timeout(partial(self._handle_input,
                                        from_input_panel,
                                        output_to,
                                        **kwargs), 100)
            return

        template = string.Template(self._expr)
        expr = template.safe_substitute({
            "from_input_panel": from_input_panel,
            "selection": Selection(self.view),
            "symbol_under_cursor": SymbolUnderCursor(self.view)})

        exprs = []
        file_name = self.view.file_name()
        if file_name:
            path = classpath_relative_path(file_name)
            file_ns = re.sub("/", ".", path)
            exprs.append("(do (load \"/" + path + "\") "
                         + "(in-ns '" + file_ns + "))")
        exprs.append(expr)

        if output_to == "repl":
            client = clients.get(wid)
            #FIXME open a new client if the client's view has been closed
            if not client:
                client = ReplClient(port)
                clients[wid] = client
        else:
            client = ReplClient(port)

        on_complete = partial(self._handle_results,
                              client = client,
                              output_to = output_to,
                              **kwargs)
        thread.start_new_thread(client.evaluate, (exprs, on_complete))

    def _handle_results(self, results, client, output_to,
            output = '$output',
            syntax_file = 'Packages/Clojure/Clojure.tmLanguage',
            view_name = '$expr'):
        if output_to == "panel":
            view = self._window.get_output_panel('clojure_output')
        elif output_to == "view":
            view = self._window.new_file()
            view.set_scratch(True)
            view.set_read_only(True)
        else:
            if not client.view:
                client.view = self._window.new_file()
                client.view.set_scratch(True)
                client.view.set_read_only(True)
                client.view.settings().set('scroll_past_end', True)

            view = client.view

        if syntax_file:
            view.set_syntax_file(syntax_file)

        result = results[-1]
        template = string.Template(output)
        output_to_view(view, template.safe_substitute(result))

        if output_to == "panel":
            self._window.run_command("show_panel",
                                     {"panel": "output.clojure_output"})
        else:
            view.sel().clear()
            view.sel().add(sublime.Region(0))

            view_name_template = string.Template(view_name)
            #FIXME uses last command's ns instead of new one
            view.set_name(view_name_template.safe_substitute(result))

            active_view = self._window.active_view()
            active_group = self._window.active_group()
            repl_view_group, _ = self._window.get_view_index(view)
            self._window.focus_view(view)
            if repl_view_group != active_group:
                # give focus back to the originally active view if it's in a
                # different group
                self._window.focus_view(active_view)

            view.set_viewport_position(view.text_to_layout(0))

class ReplServerKiller(sublime_plugin.EventListener):
    def on_close(self, view):
        wids = [str(w.id()) for w in sublime.windows()]
        servers = get_repl_servers()
        active_servers = dict((w, servers[w]) for w in wids if w in servers)
        kill_ports = set(servers.values()) - set(active_servers.values())

        # Bring out yer dead!
        for port in kill_ports:
            repl = ReplClient(port)
            thread.start_new_thread(repl.kill, ())

        if servers != active_servers:
            set_repl_servers(active_servers)