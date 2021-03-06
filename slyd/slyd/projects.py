import json, re, shutil, errno, os
from os.path import join
from twisted.web.resource import NoResource, ErrorPage
from twisted.internet.defer import Deferred
from twisted.web.server import NOT_DONE_YET
from .errors import BaseError
from .resource import SlydJsonResource
from .projecttemplates import templates
from .errors import BaseHTTPError


# stick to alphanum . and _. Do not allow only .'s (so safe for FS path)
_INVALID_PROJECT_RE = re.compile('[^A-Za-z0-9._]|^\.*$')


def create_projects_manager_resource(spec_manager):
    return ProjectsManagerResource(spec_manager)


class ProjectsManagerResource(SlydJsonResource):

    def __init__(self, spec_manager):
        SlydJsonResource.__init__(self)
        self.spec_manager = spec_manager

    def getChildWithDefault(self, project_path_element, request):
        auth_info = request.auth_info
        if ('authorized_projects' not in auth_info or
                auth_info.get('staff', False) or
                project_path_element in auth_info['authorized_projects']):
            request.project = project_path_element
            try:
                next_path_element = request.postpath.pop(0)
            except IndexError:
                next_path_element = None
            if next_path_element not in self.children:
                raise NoResource("No such child resource.")
            request.prepath.append(project_path_element)
            return self.children[next_path_element]
        else:
            return ErrorPage(
                403, "Forbidden", "You don't have access to this project.")

    def handle_project_command(self, projects_manager, command_spec):
        command = command_spec.get('cmd')
        dispatch_func = projects_manager.project_commands.get(command)
        if dispatch_func is None:
            self.bad_request(
                "unrecognised cmd arg %s, available commands: %s" %
                (command, ', '.join(projects_manager.project_commands.keys())))
        args = command_spec.get('args', [])
        try:
            retval = dispatch_func(*args)
        except TypeError:
            self.bad_request("incorrect args for %s" % command)
        except OSError as ex:
            if ex.errno == errno.ENOENT:
                self.error(404, "Not Found", "No such resource")
            elif ex.errno == errno.EEXIST or ex.errno == errno.ENOTEMPTY:
                self.error(400, "Bad Request",
                           "A project with that name already exists")
            raise
        except BaseError as ex:
            self.error(ex.status, ex.title, ex.body)
        return retval or ''

    def render_GET(self, request):
        project_manager = self.spec_manager.project_manager(request.auth_info)
        request.write(json.dumps(sorted(project_manager.list_projects())))
        return '\n'

    def render_POST(self, request):

        def finish_request(val):
            val and request.write(val)
            request.finish()

        def request_failed(failure):
            request.setResponseCode(500)
            request.write(failure.getErrorMessage())
            request.finish()
            return failure

        project_manager = self.spec_manager.project_manager(request.auth_info)
        obj = self.read_json(request)
        try:
            retval = self.handle_project_command(project_manager, obj)
            modifier = project_manager.modify_request.get(obj.get('cmd'))
            if modifier:
                print(obj)
                request = modifier(request, obj, retval)
            if isinstance(retval, Deferred):
                retval.addCallbacks(finish_request, request_failed)
                return NOT_DONE_YET
            else:
                return retval
        except BaseHTTPError as ex:
            self.error(ex.status, ex.title, ex.body)


def allowed_project_name(name):
    return not _INVALID_PROJECT_RE.search(name)


class ProjectsManager(object):

    base_dir = '.'

    @classmethod
    def setup(cls, location):
        cls.base_dir = location

    def __init__(self, auth_info):
        self.auth_info = auth_info
        self.user = auth_info['username']
        self.projectsdir = ProjectsManager.base_dir
        self.modify_request = {}
        self.project_commands = {
            'create': self.create_project,
            'mv': self.rename_project,
            'rm': self.remove_project
        }

    def all_projects(self):
        try:
            for fname in os.listdir(self.projectsdir):
                if os.path.isdir(join(self.projectsdir, fname)):
                    yield fname
        except OSError as ex:
            if ex.errno != errno.ENOENT:
                raise

    def list_projects(self):
        if 'authorized_projects' in self.auth_info:
            return self.auth_info['authorized_projects']
        else:
            return self.all_projects()

    def create_project(self, name):
        self.validate_project_name(name)
        project_filename = self.project_filename(name)
        os.makedirs(project_filename)
        with open(join(project_filename, 'project.json'), 'wb') as outf:
            outf.write(templates['PROJECT'])

        with open(join(project_filename, 'scrapy.cfg'), 'w') as outf:
            outf.write(templates['SCRAPY'])

        with open(join(project_filename, 'setup.py'), 'w') as outf:
            outf.write(templates['SETUP'] % name)

        os.makedirs(join(project_filename, 'spiders'))

        init_py = join(project_filename, 'spiders', '__init__.py')
        with open(init_py, 'w') as outf:
            outf.write('')

        settings_py = join(project_filename, 'spiders', 'settings.py')
        with open(settings_py, 'w') as outf:
            outf.write(templates['SETTINGS'])

    def rename_project(self, from_name, to_name):
        self.validate_project_name(from_name)
        self.validate_project_name(to_name)
        os.rename(self.project_filename(from_name),
                  self.project_filename(to_name))

    def remove_project(self, name):
        shutil.rmtree(self.project_filename(name))

    def project_filename(self, name):
        return join(self.projectsdir, name)

    def validate_project_name(self, name):
        if not allowed_project_name(name):
            self.error(400, 'Bad Request', 'invalid project name %s' % name)
