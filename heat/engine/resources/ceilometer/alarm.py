#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import six

from heat.common import exception
from heat.engine import constraints
from heat.engine import properties
from heat.engine import resource
from heat.engine import support
from heat.engine import watchrule


COMMON_PROPERTIES = (
    ALARM_ACTIONS, OK_ACTIONS, REPEAT_ACTIONS, INSUFFICIENT_DATA_ACTIONS,
    DESCRIPTION, ENABLED,
) = (
    'alarm_actions', 'ok_actions', 'repeat_actions',
    'insufficient_data_actions', 'description', 'enabled',
)


common_properties_schema = {
    DESCRIPTION: properties.Schema(
        properties.Schema.STRING,
        _('Description for the alarm.'),
        update_allowed=True
    ),
    ENABLED: properties.Schema(
        properties.Schema.BOOLEAN,
        _('True if alarm evaluation/actioning is enabled.'),
        default='true',
        update_allowed=True
    ),
    ALARM_ACTIONS: properties.Schema(
        properties.Schema.LIST,
        _('A list of URLs (webhooks) to invoke when state transitions to '
          'alarm.'),
        update_allowed=True
    ),
    OK_ACTIONS: properties.Schema(
        properties.Schema.LIST,
        _('A list of URLs (webhooks) to invoke when state transitions to '
          'ok.'),
        update_allowed=True
    ),
    INSUFFICIENT_DATA_ACTIONS: properties.Schema(
        properties.Schema.LIST,
        _('A list of URLs (webhooks) to invoke when state transitions to '
          'insufficient-data.'),
        update_allowed=True
    ),
    REPEAT_ACTIONS: properties.Schema(
        properties.Schema.BOOLEAN,
        _('False to trigger actions when the threshold is reached AND '
          'the alarm\'s state has changed. By default, actions are called '
          'each time the threshold is reached.'),
        default='true',
        update_allowed=True
    )
}


NOVA_METERS = ['instance', 'memory', 'memory.usage',
               'cpu', 'cpu_util', 'vcpus',
               'disk.read.requests', 'disk.read.requests.rate',
               'disk.write.requests', 'disk.write.requests.rate',
               'disk.read.bytes', 'disk.read.bytes.rate',
               'disk.write.bytes', 'disk.write.bytes.rate',
               'disk.device.read.requests', 'disk.device.read.requests.rate',
               'disk.device.write.requests', 'disk.device.write.requests.rate',
               'disk.device.read.bytes', 'disk.device.read.bytes.rate',
               'disk.device.write.bytes', 'disk.device.write.bytes.rate',
               'disk.root.size', 'disk.ephemeral.size',
               'network.incoming.bytes', 'network.incoming.bytes.rate',
               'network.outgoing.bytes', 'network.outgoing.bytes.rate',
               'network.incoming.packets', 'network.incoming.packets.rate',
               'network.outgoing.packets', 'network.outgoing.packets.rate']


def actions_to_urls(stack, properties):
    kwargs = {}
    for k, v in iter(properties.items()):
        if k in [ALARM_ACTIONS, OK_ACTIONS,
                 INSUFFICIENT_DATA_ACTIONS] and v is not None:
            kwargs[k] = []
            for act in v:
                # if the action is a resource name
                # we ask the destination resource for an alarm url.
                # the template writer should really do this in the
                # template if possible with:
                # {Fn::GetAtt: ['MyAction', 'AlarmUrl']}
                if act in stack:
                    url = stack[act].FnGetAtt('AlarmUrl')
                    kwargs[k].append(url)
                else:
                    if act:
                        kwargs[k].append(act)
        else:
            kwargs[k] = v
    return kwargs


class CeilometerAlarm(resource.Resource):

    PROPERTIES = (
        COMPARISON_OPERATOR, EVALUATION_PERIODS, METER_NAME, PERIOD,
        STATISTIC, THRESHOLD, MATCHING_METADATA,
    ) = (
        'comparison_operator', 'evaluation_periods', 'meter_name', 'period',
        'statistic', 'threshold', 'matching_metadata',
    )

    properties_schema = {
        COMPARISON_OPERATOR: properties.Schema(
            properties.Schema.STRING,
            _('Operator used to compare specified statistic with threshold.'),
            constraints=[
                constraints.AllowedValues(['ge', 'gt', 'eq', 'ne', 'lt',
                                           'le']),
            ],
            update_allowed=True
        ),
        EVALUATION_PERIODS: properties.Schema(
            properties.Schema.INTEGER,
            _('Number of periods to evaluate over.'),
            update_allowed=True
        ),
        METER_NAME: properties.Schema(
            properties.Schema.STRING,
            _('Meter name watched by the alarm.'),
            required=True
        ),
        PERIOD: properties.Schema(
            properties.Schema.INTEGER,
            _('Period (seconds) to evaluate over.'),
            update_allowed=True
        ),
        STATISTIC: properties.Schema(
            properties.Schema.STRING,
            _('Meter statistic to evaluate.'),
            constraints=[
                constraints.AllowedValues(['count', 'avg', 'sum', 'min',
                                           'max']),
            ],
            update_allowed=True
        ),
        THRESHOLD: properties.Schema(
            properties.Schema.NUMBER,
            _('Threshold to evaluate against.'),
            required=True,
            update_allowed=True
        ),
        MATCHING_METADATA: properties.Schema(
            properties.Schema.MAP,
            _('Meter should match this resource metadata (key=value) '
              'additionally to the meter_name.'),
            default={}
        ),
    }
    properties_schema.update(common_properties_schema)

    default_client_name = 'ceilometer'

    def cfn_to_ceilometer(self, stack, properties):
        kwargs = actions_to_urls(stack, properties)
        if kwargs.get(self.METER_NAME) in NOVA_METERS:
            prefix = 'user_metadata.'
        else:
            prefix = 'metering.'
        for k, v in iter(properties.items()):
            if k == self.MATCHING_METADATA:
                # make sure we have matching_metadata that looks like this:
                # matching_metadata: {metadata.$prefix.x}
                kwargs[k] = {}
                for m_k, m_v in six.iteritems(v):
                    if m_k.startswith('metadata.%s' % prefix):
                        kwargs[k][m_k] = m_v
                    elif m_k.startswith(prefix):
                        kwargs[k]['metadata.%s' % m_k] = m_v
                    else:
                        kwargs[k]['metadata.%s%s' % (prefix, m_k)] = m_v
        return kwargs

    def handle_create(self):
        props = self.cfn_to_ceilometer(self.stack,
                                       self.properties)
        props['name'] = self.physical_resource_name()

        alarm = self.ceilometer().alarms.create(**props)
        self.resource_id_set(alarm.alarm_id)

        # the watchrule below is for backwards compatibility.
        # 1) so we don't create watch tasks unneccessarly
        # 2) to support CW stats post, we will redirect the request
        #    to ceilometer.
        wr = watchrule.WatchRule(context=self.context,
                                 watch_name=self.physical_resource_name(),
                                 rule=self.parsed_template('Properties'),
                                 stack_id=self.stack.id)
        wr.state = wr.CEILOMETER_CONTROLLED
        wr.store()

    def handle_update(self, json_snippet, tmpl_diff, prop_diff):
        if prop_diff:
            kwargs = {'alarm_id': self.resource_id}
            kwargs.update(prop_diff)
            alarms_client = self.ceilometer().alarms
            alarms_client.update(**self.cfn_to_ceilometer(self.stack, kwargs))

    def handle_suspend(self):
        if self.resource_id is not None:
            self.ceilometer().alarms.update(alarm_id=self.resource_id,
                                            enabled=False)

    def handle_resume(self):
        if self.resource_id is not None:
            self.ceilometer().alarms.update(alarm_id=self.resource_id,
                                            enabled=True)

    def handle_delete(self):
        try:
            wr = watchrule.WatchRule.load(
                self.context, watch_name=self.physical_resource_name())
            wr.destroy()
        except exception.WatchRuleNotFound:
            pass

        if self.resource_id is not None:
            try:
                self.ceilometer().alarms.delete(self.resource_id)
            except Exception as ex:
                self.client_plugin().ignore_not_found(ex)


class CombinationAlarm(resource.Resource):

    support_status = support.SupportStatus(version='2014.1')

    PROPERTIES = (
        ALARM_IDS, OPERATOR,
    ) = (
        'alarm_ids', 'operator',
    )

    properties_schema = {
        ALARM_IDS: properties.Schema(
            properties.Schema.LIST,
            _('List of alarm identifiers to combine.'),
            required=True,
            constraints=[constraints.Length(min=1)],
            update_allowed=True),
        OPERATOR: properties.Schema(
            properties.Schema.STRING,
            _('Operator used to combine the alarms.'),
            constraints=[constraints.AllowedValues(['and', 'or'])],
            update_allowed=True)
    }
    properties_schema.update(common_properties_schema)

    default_client_name = 'ceilometer'

    def handle_create(self):
        properties = actions_to_urls(self.stack,
                                     self.properties)
        properties['name'] = self.physical_resource_name()
        properties['type'] = 'combination'

        alarm = self.ceilometer().alarms.create(
            **self._reformat_properties(properties))
        self.resource_id_set(alarm.alarm_id)

    def _reformat_properties(self, properties):
        combination_rule = {}
        for name in [self.ALARM_IDS, self.OPERATOR, REPEAT_ACTIONS]:
            value = properties.pop(name, None)
            if value:
                combination_rule[name] = value
        if combination_rule:
            properties['combination_rule'] = combination_rule
        return properties

    def handle_update(self, json_snippet, tmpl_diff, prop_diff):
        if prop_diff:
            kwargs = {'alarm_id': self.resource_id}
            kwargs.update(prop_diff)
            alarms_client = self.ceilometer().alarms
            alarms_client.update(**self._reformat_properties(
                actions_to_urls(self.stack, kwargs)))

    def handle_suspend(self):
        self.ceilometer().alarms.update(
            alarm_id=self.resource_id, enabled=False)

    def handle_resume(self):
        self.ceilometer().alarms.update(
            alarm_id=self.resource_id, enabled=True)

    def handle_delete(self):
        try:
            self.ceilometer().alarms.delete(self.resource_id)
        except Exception as ex:
            self.client_plugin().ignore_not_found(ex)


def resource_mapping():
    return {
        'OS::Ceilometer::Alarm': CeilometerAlarm,
        'OS::Ceilometer::CombinationAlarm': CombinationAlarm,
    }
