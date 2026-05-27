from python.helpers.api import ApiHandler, Input, Output, Request, Response
from python.helpers import projects
from python.helpers.notification import NotificationManager, NotificationType, NotificationPriority
from hyperagent0 import projects as hp_projects


class Projects(ApiHandler):
    async def process(self, input: Input, request: Request) -> Output:
        action = input.get("action", "")
        ctxid = input.get("context_id", None)

        if ctxid:
            _context = self.use_context(ctxid)

        try:
            if action == "list":
                data = self.get_active_projects_list()
            elif action == "list_options":
                data = self.get_active_projects_options()
            elif action == "load":
                data = self.load_project(input.get("name", None))
            elif action == "create":
                data = self.create_project(input.get("project", None))
            elif action == "clone":
                data = self.clone_project(input.get("project", None))
            elif action == "update":
                data = self.update_project(input.get("project", None))
            elif action == "delete":
                data = self.delete_project(input.get("name", None))
            elif action == "activate":
                data = self.activate_project(ctxid, input.get("name", None))
            elif action == "deactivate":
                data = self.deactivate_project(ctxid)
            elif action == "file_structure":
                data = self.get_file_structure(input.get("name", None), input.get("settings"))
            # Spec 10 P2 — per-project capability editors.
            elif action == "mcp_get":
                data = self.get_project_mcp(input.get("name", None))
            elif action == "mcp_set":
                data = self.set_project_mcp(input.get("name", None), input.get("payload", None))
            elif action == "network_get":
                data = self.get_project_network(input.get("name", None))
            elif action == "network_set":
                data = self.set_project_network(input.get("name", None), input.get("allow", None))
            else:
                raise Exception("Invalid action")

            return {
                "ok": True,
                "data": data,
            }
        except Exception as e:
            return {
                "ok": False,
                "error": str(e),
            }

    def get_active_projects_list(self):
        return projects.get_active_projects_list()

    def get_active_projects_options(self):
        items = projects.get_active_projects_list() or []
        return [
            {"key": p.get("name", ""), "label": p.get("title", "") or p.get("name", "")}
            for p in items
            if p.get("name")
        ]

    def create_project(self, project: dict|None):
        if project is None:
            raise Exception("Project data is required")
        data = projects.BasicProjectData(**project)
        name = projects.create_project(project["name"], data)
        return projects.load_edit_project_data(name)

    def clone_project(self, project: dict|None):
        if project is None:
            raise Exception("Project data is required")
        git_url = project.get("git_url", "")
        git_token = project.get("git_token", "")
        if not git_url:
            raise Exception("Git URL is required")
        
        # Progress notification
        notification = NotificationManager.send_notification(
            NotificationType.PROGRESS,
            NotificationPriority.NORMAL,
            f"Cloning repository...",
            "Git Clone",
            display_time=999,
            group="git_clone"
        )
        
        try:
            data = projects.BasicProjectData(**project)
            name = projects.clone_git_project(project["name"], git_url, git_token, data)
            
            # Success notification
            NotificationManager.send_notification(
                NotificationType.SUCCESS,
                NotificationPriority.NORMAL,
                f"Repository cloned successfully",
                "Git Clone",
                display_time=3,
                group="git_clone"
            )
            return projects.load_edit_project_data(name)
        except Exception as e:
            # Error notification
            NotificationManager.send_notification(
                NotificationType.ERROR,
                NotificationPriority.HIGH,
                f"Clone failed: {str(e)}",
                "Git Clone",
                display_time=5,
                group="git_clone"
            )
            raise

    def load_project(self, name: str|None):
        if name is None:
            raise Exception("Project name is required")
        return projects.load_edit_project_data(name)

    def update_project(self, project: dict|None):
        if project is None:
            raise Exception("Project data is required")
        data = projects.EditProjectData(**project)
        name = projects.update_project(project["name"], data)
        return projects.load_edit_project_data(name)

    def delete_project(self, name: str|None):
        if name is None:
            raise Exception("Project name is required")
        return projects.delete_project(name)

    def activate_project(self, context_id: str|None, name: str|None):
        if not context_id:
            raise Exception("Context ID is required")
        if not name:
            raise Exception("Project name is required") 
        return projects.activate_project(context_id, name)

    def deactivate_project(self, context_id: str|None):
        if not context_id:
            raise Exception("Context ID is required")
        return projects.deactivate_project(context_id)

    def get_file_structure(self, name: str|None, settings: dict|None):
        if not name:
            raise Exception("Project name is required")
        # project data
        basic_data = projects.load_basic_project_data(name)
        # override file structure settings
        if settings:
            basic_data["file_structure"] = settings # type: ignore
        # get structure
        return projects.get_file_structure(name, basic_data)

    # ------------------------------------------------------------------
    # Spec 10 P2 — per-project capability editors (MCP + network)
    # ------------------------------------------------------------------

    def get_project_mcp(self, name: str | None):
        if not name:
            raise Exception("Project name is required")
        payload = hp_projects.load_project_mcp_servers(name)
        # ``None`` is the fall-through-to-global signal; the UI renders it
        # as an empty editor with a "using global MCP" hint.
        return {"name": name, "payload": payload or "", "uses_global": payload is None}

    def set_project_mcp(self, name: str | None, payload):
        if not name:
            raise Exception("Project name is required")
        if payload is not None and not isinstance(payload, str):
            raise Exception("payload must be a string (raw JSON text) or null")
        try:
            hp_projects.save_project_mcp_servers(name, payload)
        except ValueError as exc:
            # Surface JSON parse errors as a structured editor error.
            raise Exception(str(exc)) from exc
        # Round-trip back the persisted state so the UI can refresh
        # without a second fetch.
        return self.get_project_mcp(name)

    def get_project_network(self, name: str | None):
        if not name:
            raise Exception("Project name is required")
        return {"name": name, "allow": hp_projects.load_project_network_allow(name)}

    def set_project_network(self, name: str | None, allow):
        if not name:
            raise Exception("Project name is required")
        if allow is None:
            allow = []
        if not isinstance(allow, list):
            raise Exception("allow must be a list of host strings")
        written = hp_projects.save_project_network_allow(name, allow)
        return {"name": name, "allow": written}