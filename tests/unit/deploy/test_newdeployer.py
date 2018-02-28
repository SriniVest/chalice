import os
import unittest

from attr import attrs, attrib
import pytest
from pytest import fixture
import mock
import botocore.session

from chalice.awsclient import TypedAWSClient, ResourceDoesNotExistError
from chalice.utils import OSUtils
from chalice.deploy import models
from chalice.deploy import packager
from chalice.config import Config
from chalice.app import Chalice
from chalice.deploy.newdeployer import create_default_deployer
from chalice.deploy.newdeployer import Deployer
from chalice.deploy.newdeployer import BaseDeployStep
from chalice.deploy.newdeployer import BuildStage
from chalice.deploy.newdeployer import DependencyBuilder
from chalice.deploy.newdeployer import ApplicationGraphBuilder
from chalice.deploy.newdeployer import InjectDefaults, DeploymentPackager
from chalice.deploy.newdeployer import PolicyGenerator
from chalice.deploy.newdeployer import PlanStage
from chalice.deploy.newdeployer import Executor, Variable
from chalice.deploy.newdeployer import UnresolvedValueError
from chalice.deploy.models import APICall
from chalice.policy import AppPolicyGenerator
from chalice.constants import LAMBDA_TRUST_POLICY


@attrs
class FooResource(models.Model):
    name = attrib()
    leaf = attrib()

    def dependencies(self):
        return [self.leaf]


@attrs
class LeafResource(models.Model):
    name = attrib()


@fixture
def lambda_app():
    app = Chalice('lambda-only')

    @app.lambda_function()
    def foo(event, context):
        return {}

    return app


@fixture
def mock_client():
    return mock.Mock(spec=TypedAWSClient)


@fixture
def mock_osutils():
    return mock.Mock(spec=OSUtils)


def create_function_resource(name):
    return models.LambdaFunction(
        resource_name=name,
        function_name='appname-dev-%s' % name,
        environment_variables={},
        runtime='python2.7',
        handler='app.app',
        tags={},
        timeout=60,
        memory_size=128,
        deployment_package=models.DeploymentPackage(filename='foo'),
        role=models.PreCreatedIAMRole(role_arn='role:arn')
    )


def test_can_build_resource_with_single_dep():
    role = models.PreCreatedIAMRole(role_arn='foo')
    app = models.Application(stage='dev', resources=[role])

    dep_builder = DependencyBuilder()
    deps = dep_builder.build_dependencies(app)
    assert deps == [role]


def test_can_build_resource_with_dag_deps():
    shared_leaf = LeafResource(name='leaf-resource')
    first_parent = FooResource(name='first', leaf=shared_leaf)
    second_parent = FooResource(name='second', leaf=shared_leaf)
    app = models.Application(
        stage='dev', resources=[first_parent, second_parent])

    dep_builder = DependencyBuilder()
    deps = dep_builder.build_dependencies(app)
    assert deps == [shared_leaf, first_parent, second_parent]


class TestApplicationGraphBuilder(object):

    def create_config(self, app, iam_role_arn=None, policy_file=None,
                      autogen_policy=False):
        kwargs = {
            'chalice_app': app,
            'app_name': 'lambda-only',
            'project_dir': '.',
        }
        if iam_role_arn is not None:
            # We want to use an existing role.
            # This will skip all the autogen-policy
            # and role creation.
            kwargs['manage_iam_role'] = False
            kwargs['iam_role_arn'] = 'role:arn'
        elif policy_file is not None:
            # Otherwise this setting is when a user wants us to
            # manage the role, but they've written a policy file
            # they'd like us to use.
            kwargs['autogen_policy'] = False
            kwargs['iam_policy_file'] = policy_file
        elif autogen_policy:
            kwargs['autogen_policy'] = True
        config = Config.create(**kwargs)
        return config

    def test_can_build_single_lambda_function_app(self, lambda_app):
        # This is the simplest configuration we can get.
        builder = ApplicationGraphBuilder()
        config = self.create_config(lambda_app, iam_role_arn='role:arn')
        application = builder.build(config, stage_name='dev')
        # The top level resource is always an Application.
        assert isinstance(application, models.Application)
        assert len(application.resources) == 1
        assert application.resources[0] == models.LambdaFunction(
            resource_name='foo',
            function_name='lambda-only-dev-foo',
            environment_variables={},
            runtime=config.lambda_python_version,
            handler='app.foo',
            tags=config.tags,
            timeout=None,
            memory_size=None,
            deployment_package=models.DeploymentPackage(
                models.Placeholder.BUILD_STAGE),
            role=models.PreCreatedIAMRole('role:arn'),
        )

    def test_multiple_lambda_functions_share_role_and_package(self,
                                                              lambda_app):
        # We're going to add another lambda_function to our app.
        @lambda_app.lambda_function()
        def bar(event, context):
            return {}

        builder = ApplicationGraphBuilder()
        config = self.create_config(lambda_app, iam_role_arn='role:arn')
        application = builder.build(config, stage_name='dev')
        assert len(application.resources) == 2
        # The lambda functions by default share the same role
        assert application.resources[0].role == application.resources[1].role
        # And all lambda functions share the same deployment package.
        assert (application.resources[0].deployment_package ==
                application.resources[1].deployment_package)

    def test_autogen_policy_for_function(self, lambda_app):
        # This test is just a sanity test that verifies all the params
        # for an ManagedIAMRole.  The various combinations for role
        # configuration is all tested via RoleTestCase.
        config = self.create_config(lambda_app, autogen_policy=True)
        builder = ApplicationGraphBuilder()
        application = builder.build(config, stage_name='dev')
        function = application.resources[0]
        role = function.role
        # We should have linked a ManagedIAMRole
        assert isinstance(role, models.ManagedIAMRole)
        assert role == models.ManagedIAMRole(
            resource_name='default-role',
            role_arn=models.Placeholder.DEPLOY_STAGE,
            role_name='lambda-only-dev',
            trust_policy=LAMBDA_TRUST_POLICY,
            policy=models.AutoGenIAMPolicy(models.Placeholder.BUILD_STAGE),
        )


class RoleTestCase(object):
    def __init__(self, given, roles, app_name='appname'):
        self.given = given
        self.roles = roles
        self.app_name = app_name

    def build(self):
        app = Chalice(self.app_name)

        for name in self.given:
            def foo(event, context):
                return {}
            foo.__name__ = name
            app.lambda_function(name)(foo)

        user_provided_params = {
            'chalice_app': app,
            'app_name': self.app_name,
            'project_dir': '.',
        }
        lambda_functions = {}
        for key, value in self.given.items():
            lambda_functions[key] = value
        config_from_disk = {
            'stages': {
                'dev': {
                    'lambda_functions': lambda_functions,
                }
            }
        }
        config = Config(chalice_stage='dev',
                        user_provided_params=user_provided_params,
                        config_from_disk=config_from_disk)
        return app, config

    def assert_required_roles_created(self, application):
        resources = application.resources
        assert len(resources) == len(self.given)
        functions_by_name = {f.function_name: f for f in resources}
        for function_name, expected in self.roles.items():
            full_name = 'appname-dev-%s' % function_name
            assert full_name in functions_by_name
            actual_role = functions_by_name[full_name].role
            expectations = self.roles[function_name]
            if not expectations.get('managed_role', True):
                assert isinstance(actual_role, models.PreCreatedIAMRole)
                assert expectations['iam_role_arn'] == actual_role.role_arn
                continue
            assert expectations['name'] == actual_role.role_name

            is_autogenerated = expectations.get('autogenerated', False)
            policy_file = expectations.get('policy_file')
            if is_autogenerated:
                assert isinstance(actual_role, models.ManagedIAMRole)
                assert isinstance(actual_role.policy, models.AutoGenIAMPolicy)
            if policy_file is not None and not is_autogenerated:
                assert isinstance(actual_role, models.ManagedIAMRole)
                assert isinstance(actual_role.policy,
                                  models.FileBasedIAMPolicy)
                assert actual_role.policy.filename == os.path.join(
                    '.', '.chalice', expectations['policy_file'])


# How to read these tests:
# 'given' is a mapping of lambda function name to config values.
# 'roles' is a mapping of lambda function to expected attributes
# of the role associated with the given function.
# The first test case is explained in more detail as an example.
ROLE_TEST_CASES = [
    # Default case, we use the shared 'appname-dev' role.
    RoleTestCase(
        # Given we have a lambda function in our app.py named 'a',
        # and we have our config file state that the 'a' function
        # should have an autogen'd policy,
        given={'a': {'autogen_policy': True}},
        # then we expect the IAM role associated with the lambda
        # function 'a' should be named 'appname-dev', and it should
        # be an autogenerated role/policy.
        roles={'a': {'name': 'appname-dev', 'autogenerated': True}}),
    # If you specify an explicit policy, we generate a function
    # specific role.
    RoleTestCase(
        given={'a': {'autogen_policy': False,
                     'iam_policy_file': 'mypolicy.json'}},
        roles={'a': {'name': 'appname-dev-a',
                     'autogenerated': False,
                     'policy_file': 'mypolicy.json'}}),
    # Multiple lambda functions that use autogen policies share
    # the same 'appname-dev' role.
    RoleTestCase(
        given={'a': {'autogen_policy': True},
               'b': {'autogen_policy': True}},
        roles={'a': {'name': 'appname-dev'},
               'b': {'name': 'appname-dev'}}),
    # Multiple lambda functions with separate policies result
    # in separate roles.
    RoleTestCase(
        given={'a': {'autogen_policy': False,
                     'iam_policy_file': 'a.json'},
               'b': {'autogen_policy': False,
                     'iam_policy_file': 'b.json'}},
        roles={'a': {'name': 'appname-dev-a',
                     'autogenerated': False,
                     'policy_file': 'a.json'},
               'b': {'name': 'appname-dev-b',
                     'autogenerated': False,
                     'policy_file': 'b.json'}}),
    # You can mix autogen and explicit policy files.  Autogen will
    # always use the '{app}-{stage}' role.
    RoleTestCase(
        given={'a': {'autogen_policy': True},
               'b': {'autogen_policy': False,
                     'iam_policy_file': 'b.json'}},
        roles={'a': {'name': 'appname-dev',
                     'autogenerated': True},
               'b': {'name': 'appname-dev-b',
                     'autogenerated': False,
                     'policy_file': 'b.json'}}),
    # Default location if no policy file is given is
    # policy-dev.json
    RoleTestCase(
        given={'a': {'autogen_policy': False}},
        roles={'a': {'name': 'appname-dev-a',
                     'autogenerated': False,
                     'policy_file': 'policy-dev.json'}}),
    # As soon as autogen_policy is false, we will *always*
    # create a function specific role.
    RoleTestCase(
        given={'a': {'autogen_policy': False},
               'b': {'autogen_policy': True}},
        roles={'a': {'name': 'appname-dev-a',
                     'autogenerated': False,
                     'policy_file': 'policy-dev.json'},
               'b': {'name': 'appname-dev'}}),
    RoleTestCase(
        given={'a': {'manage_iam_role': False, 'iam_role_arn': 'role:arn'}},
        # 'managed_role' will verify the associated role is a
        # models.PreCreatedIAMRoleType with the provided iam_role_arn.
        roles={'a': {'managed_role': False, 'iam_role_arn': 'role:arn'}}),
    RoleTestCase(
        given={'a': {'manage_iam_role': False, 'iam_role_arn': 'role:arn'},
               'b': {'autogen_policy': True}},
        roles={'a': {'managed_role': False, 'iam_role_arn': 'role:arn'},
               'b': {'name': 'appname-dev', 'autogenerated': True}}),

    # Functions that mix all four options:
    RoleTestCase(
        # 2 functions with autogen'd policies.
        given={
            'a': {'autogen_policy': True},
            'b': {'autogen_policy': True},
            # 2 functions with various iam role arns.
            'c': {'manage_iam_role': False, 'iam_role_arn': 'role:arn'},
            'd': {'manage_iam_role': False, 'iam_role_arn': 'role:arn2'},
            # A function with a default filename for a policy.
            'e': {'autogen_policy': False},
            # Even though this uses the same policy as 'e', we will
            # still create a new role.  This could be optimized in the
            # future.
            'f': {'autogen_policy': False},
            # And finally 2 functions that have their own policy files.
            'g': {'autogen_policy': False, 'iam_policy_file': 'g.json'},
            'h': {'autogen_policy': False, 'iam_policy_file': 'h.json'}
        },
        roles={
            'a': {'name': 'appname-dev', 'autogenerated': True},
            'b': {'name': 'appname-dev', 'autogenerated': True},
            'c': {'managed_role': False, 'iam_role_arn': 'role:arn'},
            'd': {'managed_role': False, 'iam_role_arn': 'role:arn2'},
            'e': {'name': 'appname-dev-e',
                  'autogenerated': False,
                  'policy_file': 'policy-dev.json'},
            'f': {'name': 'appname-dev-f',
                  'autogenerated': False,
                  'policy_file': 'policy-dev.json'},
            'g': {'name': 'appname-dev-g',
                  'autogenerated': False,
                  'policy_file': 'g.json'},
            'h': {'name': 'appname-dev-h',
                  'autogenerated': False,
                  'policy_file': 'h.json'},
        }),
]


@pytest.mark.parametrize('case', ROLE_TEST_CASES)
def test_role_creation(case):
    _, config = case.build()
    builder = ApplicationGraphBuilder()
    application = builder.build(config, stage_name='dev')
    case.assert_required_roles_created(application)


class TestDefaultsInjector(object):
    def test_inject_when_values_are_none(self):
        injector = InjectDefaults(
            lambda_timeout=100,
            lambda_memory_size=512,
        )
        function = models.LambdaFunction(
            # The timeout/memory_size are set to
            # None, so the injector should fill them
            # in the with the default values above.
            timeout=None,
            memory_size=None,
            resource_name='foo',
            function_name='app-dev-foo',
            environment_variables={},
            runtime='python2.7',
            handler='app.app',
            tags={},
            deployment_package=None,
            role=None,
        )
        config = Config.create()
        injector.handle(config, function)
        assert function.timeout == 100
        assert function.memory_size == 512

    def test_no_injection_when_values_are_set(self):
        injector = InjectDefaults(
            lambda_timeout=100,
            lambda_memory_size=512,
        )
        function = models.LambdaFunction(
            # The timeout/memory_size are set to
            # None, so the injector should fill them
            # in the with the default values above.
            timeout=1,
            memory_size=1,
            resource_name='foo',
            function_name='app-stage-foo',
            environment_variables={},
            runtime='python2.7',
            handler='app.app',
            tags={},
            deployment_package=None,
            role=None,
        )
        config = Config.create()
        injector.handle(config, function)
        assert function.timeout == 1
        assert function.memory_size == 1


class TestPolicyGeneratorStage(object):
    def test_invokes_policy_generator(self):
        generator = mock.Mock(spec=AppPolicyGenerator)
        generator.generate_policy.return_value = {'policy': 'doc'}
        policy = models.AutoGenIAMPolicy(models.Placeholder.BUILD_STAGE)
        config = Config.create()

        p = PolicyGenerator(generator)
        p.handle(config, policy)

        assert policy.document == {'policy': 'doc'}

    def test_no_policy_generated_if_exists(self):
        generator = mock.Mock(spec=AppPolicyGenerator)
        generator.generate_policy.return_value = {'policy': 'new'}
        policy = models.AutoGenIAMPolicy(document={'policy': 'original'})
        config = Config.create()

        p = PolicyGenerator(generator)
        p.handle(config, policy)

        assert policy.document == {'policy': 'original'}
        assert not generator.generate_policy.called


class TestDeploymentPackager(object):
    def test_can_generate_package(self):
        generator = mock.Mock(spec=packager.LambdaDeploymentPackager)
        generator.create_deployment_package.return_value = 'package.zip'

        package = models.DeploymentPackage(models.Placeholder.BUILD_STAGE)
        config = Config.create()

        p = DeploymentPackager(generator)
        p.handle(config, package)

        assert package.filename == 'package.zip'

    def test_package_not_generated_if_filename_populated(self):
        generator = mock.Mock(spec=packager.LambdaDeploymentPackager)
        generator.create_deployment_package.return_value = 'NEWPACKAGE.zip'

        package = models.DeploymentPackage(filename='original-name.zip')
        config = Config.create()

        p = DeploymentPackager(generator)
        p.handle(config, package)

        assert package.filename == 'original-name.zip'
        assert not generator.create_deployment_package.called


class TestPlanStageCreate(object):
    def test_can_plan_for_iam_role_creation(self, mock_client, mock_osutils):
        mock_client.get_role_arn_for_name.side_effect = \
            ResourceDoesNotExistError()
        planner = PlanStage(mock_client, mock_osutils)
        resource = models.ManagedIAMRole(
            resource_name='default-role',
            role_arn=models.Placeholder.DEPLOY_STAGE,
            role_name='myrole',
            trust_policy={'trust': 'policy'},
            policy=models.AutoGenIAMPolicy(document={'iam': 'policy'}),
        )
        plan = planner.execute(Config.create(), [resource])
        assert len(plan) == 1
        api_call = plan[0]
        assert api_call.method_name == 'create_role'
        assert api_call.params == {'name': 'myrole',
                                   'trust_policy': {'trust': 'policy'},
                                   'policy': {'iam': 'policy'}}
        assert api_call.target_variable == 'myrole_role_arn'
        assert api_call.resource == resource

    def test_can_create_plan_for_filebased_role(self, mock_client,
                                                mock_osutils):
        mock_client.get_role_arn_for_name.side_effect = \
                ResourceDoesNotExistError
        resource = models.ManagedIAMRole(
            resource_name='default-role',
            role_arn=models.Placeholder.DEPLOY_STAGE,
            role_name='myrole',
            trust_policy={'trust': 'policy'},
            policy=models.FileBasedIAMPolicy(filename='foo.json'),
        )
        mock_osutils.get_file_contents.return_value = '{"iam": "policy"}'
        planner = PlanStage(mock_client, mock_osutils)
        plan = planner.execute(Config.create(project_dir='.'), [resource])
        assert len(plan) == 1
        api_call = plan[0]
        assert api_call.method_name == 'create_role'
        assert api_call.params == {'name': 'myrole',
                                   'trust_policy': {'trust': 'policy'},
                                   'policy': {'iam': 'policy'}}
        assert api_call.target_variable == 'myrole_role_arn'
        assert api_call.resource == resource

    def test_can_create_function(self, mock_client, mock_osutils):
        mock_client.lambda_function_exists.return_value = False
        function = create_function_resource('function_name')
        planner = PlanStage(mock_client, mock_osutils)
        plan = planner.execute(Config.create(), [function])
        assert len(plan) == 1
        call = plan[0]
        assert call.method_name == 'create_function'
        assert call.target_variable == 'function_name_lambda_arn'
        assert call.params == {
            'function_name': 'appname-dev-function_name',
            'role_arn': 'role:arn',
            'zip_contents': mock.ANY,
            'runtime': 'python2.7',
            'handler': 'app.app',
            'environment_variables': {},
            'tags': {},
            'timeout': 60,
            'memory_size': 128,
        }
        assert call.resource == function

    def test_can_create_plan_for_managed_role(self, mock_client, mock_osutils):
        mock_client.lambda_function_exists.return_value = False
        mock_client.get_role_arn_for_name.side_effect = \
            ResourceDoesNotExistError
        function = create_function_resource('function_name')
        function.role = models.ManagedIAMRole(
            resource_name='myrole',
            role_arn=models.Placeholder.DEPLOY_STAGE,
            role_name='myrole-dev',
            trust_policy={'trust': 'policy'},
            policy=models.FileBasedIAMPolicy(filename='foo.json'),
        )
        planner = PlanStage(mock_client, mock_osutils)
        plan = planner.execute(Config.create(), [function])
        assert len(plan) == 1
        call = plan[0]
        assert call.method_name == 'create_function'
        assert call.target_variable == 'function_name_lambda_arn'
        assert call.resource == function
        # The params are verified in test_can_create_function,
        # we just care about how the role_arn Variable is constructed.
        role_arn = call.params['role_arn']
        assert isinstance(role_arn, Variable)
        assert role_arn.name == 'myrole-dev_role_arn'


class TestPlanStageUpdate(object):
    def test_can_update_lambda_function_code(self, mock_client, mock_osutils):
        mock_client.lambda_function_exists.return_value = True
        function = create_function_resource('function_name')
        # Now let's change the memory size and ensure we
        # get an update.
        function.memory_size = 256
        planner = PlanStage(mock_client, mock_osutils)
        plan = planner.execute(Config.create(), [function])
        assert len(plan) == 1
        call = plan[0]
        assert call.method_name == 'update_function'
        assert call.resource == function
        # We don't need to set a target variable because the
        # function already exists and we know the arn.
        assert call.target_variable is None
        existing_params = {
            'function_name': 'appname-dev-function_name',
            'role_arn': 'role:arn',
            'zip_contents': mock.ANY,
            'runtime': 'python2.7',
            'environment_variables': {},
            'tags': {},
            'timeout': 60,
        }
        expected = dict(memory_size=256, **existing_params)
        assert call.params == expected

    def test_can_update_managed_role(self, mock_client, mock_osutils):
        mock_client.get_role_arn_for_name.return_value = 'myrole:arn'
        role = models.ManagedIAMRole(
            resource_name='resource_name',
            role_arn='myrole:arn',
            role_name='myrole',
            trust_policy={},
            policy=models.AutoGenIAMPolicy(document={'role': 'policy'}),
        )
        planner = PlanStage(mock_client, mock_osutils)
        plan = planner.execute(Config.create(), [role])
        assert len(plan) == 2
        delete_call = plan[0]
        assert delete_call.method_name == 'delete_role_policy'
        assert delete_call.params == {'role_name': 'myrole',
                                      'policy_name': 'myrole'}
        assert delete_call.resource == role

        update_call = plan[1]
        assert update_call.method_name == 'put_role_policy'
        assert update_call.params == {'role_name': 'myrole',
                                      'policy_name': 'myrole',
                                      'policy_document': {'role': 'policy'}}
        assert update_call.resource == role

    def test_can_update_file_based_policy(self, mock_client, mock_osutils):
        mock_client.get_role_arn_for_name.return_value = 'myrole:arn'
        role = models.ManagedIAMRole(
            resource_name='resource_name',
            role_arn='myrole:arn',
            role_name='myrole',
            trust_policy={},
            policy=models.FileBasedIAMPolicy(filename='foo.json'),
        )
        mock_osutils.get_file_contents.return_value = '{"iam": "policy"}'
        planner = PlanStage(mock_client, mock_osutils)
        plan = planner.execute(Config.create(), [role])
        assert len(plan) == 2
        delete_call = plan[0]
        assert delete_call.method_name == 'delete_role_policy'
        assert delete_call.params == {'role_name': 'myrole',
                                      'policy_name': 'myrole'}
        assert delete_call.resource == role

        update_call = plan[1]
        assert update_call.method_name == 'put_role_policy'
        assert update_call.params == {'role_name': 'myrole',
                                      'policy_name': 'myrole',
                                      'policy_document': {'iam': 'policy'}}
        assert update_call.resource == role

    def test_no_update_for_non_managed_role(self):
        role = models.PreCreatedIAMRole(role_arn='role:arn')
        planner = PlanStage(mock_client, mock_osutils)
        plan = planner.execute(Config.create(), [role])
        assert plan == []

    def test_can_update_with_placeholder_but_exists(self, mock_client,
                                                    mock_osutils):
        mock_client.get_role_arn_for_name.return_value = 'myrole:arn'
        role = models.ManagedIAMRole(
            resource_name='resource_name',
            role_arn=models.Placeholder.DEPLOY_STAGE,
            role_name='myrole',
            trust_policy={},
            policy=models.AutoGenIAMPolicy(document={'role': 'policy'}),
        )
        planner = PlanStage(mock_client, mock_osutils)
        plan = planner.execute(Config.create(), [role])
        assert len(plan) == 2
        delete_call = plan[0]
        assert delete_call.method_name == 'delete_role_policy'
        assert delete_call.params == {'role_name': 'myrole',
                                      'policy_name': 'myrole'}
        assert delete_call.resource == role

        update_call = plan[1]
        assert update_call.method_name == 'put_role_policy'
        assert update_call.params == {'role_name': 'myrole',
                                      'policy_name': 'myrole',
                                      'policy_document': {'role': 'policy'}}
        assert update_call.resource == role

        assert role.role_arn == 'myrole:arn'


class TestInvoker(object):
    def test_can_invoke_api_call_with_no_output(self, mock_client):
        params = {'name': 'foo', 'trust_policy': {'trust': 'policy'},
                  'policy': {'iam': 'policy'}}
        call = APICall('create_role', params, target_variable=None)

        executor = Executor(mock_client)
        executor.execute([call])

        mock_client.create_role.assert_called_with(**params)

    def test_can_store_api_result(self, mock_client):
        params = {'name': 'foo', 'trust_policy': {'trust': 'policy'},
                  'policy': {'iam': 'policy'}}
        call = APICall('create_role', params,
                       target_variable='my_variable_name')
        mock_client.create_role.return_value = 'myrole:arn'

        executor = Executor(mock_client)
        executor.execute([call])

        assert executor.variables['my_variable_name'] == 'myrole:arn'

    def test_can_reference_stored_results_in_api_calls(self, mock_client):
        params = {
            'name': Variable('role_name'),
            'trust_policy': {'trust': 'policy'},
            'policy': {'iam': 'policy'}
        }
        call = APICall('create_role', params,
                       target_variable='my_variable_name')
        mock_client.create_role.return_value = 'myrole:arn'

        executor = Executor(mock_client)
        executor.variables['role_name'] = 'myrole-name'
        executor.execute([call])

        mock_client.create_role.assert_called_with(
            name='myrole-name',
            trust_policy={'trust': 'policy'},
            policy={'iam': 'policy'},
        )

    def test_can_return_created_resources(self, mock_client):
        function = create_function_resource('myfunction')
        params = {}
        call = APICall('create_function', params,
                       target_variable='myfunction_arn',
                       resource=function)
        mock_client.create_function.return_value = 'function:arn'
        executor = Executor(mock_client)
        executor.execute([call])
        assert executor.resources['myfunction'] == {
            'myfunction_arn': 'function:arn',
            'resource_type': 'lambda_function',
        }

    def test_validates_no_unresolved_deploy_vars(self, mock_client):
        function = create_function_resource('myfunction')
        params = {'zip_contents': models.Placeholder.BUILD_STAGE}
        call = APICall('create_function', params,
                       target_variable='myfunction_arn',
                       resource=function)
        mock_client.create_function.return_value = 'function:arn'
        executor = Executor(mock_client)
        # We should raise an exception because a param has
        # a models.Placeholder.BUILD_STAGE value which should have
        # been handled in an earlier stage.
        with pytest.raises(UnresolvedValueError):
            executor.execute([call])


def test_build_stage():
    first = mock.Mock(spec=BaseDeployStep)
    second = mock.Mock(spec=BaseDeployStep)
    build = BuildStage([first, second])

    foo_resource = mock.sentinel.foo_resource
    bar_resource = mock.sentinel.bar_resource
    config = Config.create()
    build.execute(config, [foo_resource, bar_resource])

    assert first.handle.call_args_list == [
        mock.call(config, foo_resource),
        mock.call(config, bar_resource),
    ]
    assert second.handle.call_args_list == [
        mock.call(config, foo_resource),
        mock.call(config, bar_resource),
    ]


class TestDeployer(unittest.TestCase):
    def setUp(self):
        self.resource_builder = mock.Mock(spec=ApplicationGraphBuilder)
        self.deps_builder = mock.Mock(spec=DependencyBuilder)
        self.build_stage = mock.Mock(spec=BuildStage)
        self.plan_stage = mock.Mock(spec=PlanStage)
        self.executor = mock.Mock(spec=Executor)

    def create_deployer(self):
        return Deployer(
            self.resource_builder,
            self.deps_builder,
            self.build_stage,
            self.plan_stage,
            self.executor
        )

    def test_deploy_delegates_properly(self):
        app = mock.Mock(spec=models.Application)
        resources = [mock.Mock(spec=models.Model)]
        api_calls = [mock.Mock(spec=APICall)]

        self.resource_builder.build.return_value = app
        self.deps_builder.build_dependencies.return_value = resources
        self.plan_stage.execute.return_value = api_calls
        self.executor.resources = {'foo': {'name': 'bar'}}

        deployer = self.create_deployer()
        config = Config.create()
        result = deployer.deploy(config, 'dev')

        self.resource_builder.build.assert_called_with(config, 'dev')
        self.deps_builder.build_dependencies.assert_called_with(app)
        self.build_stage.execute.assert_called_with(config, resources)
        self.plan_stage.execute.assert_called_with(config, resources)
        self.executor.execute.assert_called_with(api_calls)

        assert result == {'resources': {'foo': {'name': 'bar'}}}


def test_can_create_default_deployer():
    session = botocore.session.get_session()
    deployer = create_default_deployer(session)
    assert isinstance(deployer, Deployer)
