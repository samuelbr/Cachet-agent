#!/usr/bin/env python

import os
import re
import json
import time
import logging
import argparse
import cachetclient.cachet as cachet
import requests
import sys

"""
pip install python-cachetclient

Config format:
GroupName,ComponentName,CheckType,params1,params2,...

"""

class CachetAgent(object):
  def __init__(self, endpoint, api_token, check_interval=60, conf_file='agent.conf', conf_data=None):
    self.logger = logging.getLogger('cachetAgent')
    
    self.conf_file = conf_file
    self.conf_data = conf_data
    
    self.ENDPOINT = endpoint
    self.API_TOKEN = api_token
    self.CHECK_INTERVAL = check_interval
    
    self.logger.info('Cachet endpoint: %s', self.ENDPOINT)
    self.logger.info('Check interval: %ds', self.CHECK_INTERVAL)
    
    self._group_cache = {}
    self._probes = {}
    
    self._create_probes()
    
  def _create_probes(self):
    if self.conf_data is None:
      probes_factory = self._create_probes_from_file
      self.logger.info('Load config from %s', self.conf_file)
    else:
      probes_factory = self._create_probes_from_data
      self.logger.info('Load data from OS environment')
    
    for component_id, probe in probes_factory():
      self.logger.debug('Created probe %s for component id %d', probe, component_id)
      self._probes[component_id] = probe    
  
  def _create_probes_from_file(self):
    with open(self.conf_file, 'r') as conf_file:
      for line in conf_file.readlines():
        result = self._create_probe(line.strip())
        if result:
          yield result
  
  def _create_probes_from_data(self):
    for line in re.split('[\r\n]+', self.conf_data):
      result = self._create_probe(line.strip())
      if result:
        yield result
          
  def _create_probe(self, agent_conf):
    if len(agent_conf) == 0 or agent_conf[0] == '#':
      #comment or empty line
      return None
    
    config_parts = list(map(str.strip, agent_conf.split(',')))
    
    group_name = config_parts[0]
    component_name = config_parts[1]
    probe_name = config_parts[2]
    
    group_id = self._get_or_create_group(group_name)
    component_id = self._get_or_create_component(group_id, component_name)
    
    if probe_name == 'SpringBoot':
      return component_id, SpringBootProbe(config_parts[3:])
      
    else:
      raise Exception('Unsupported probe '+probe_name)
    
  def _get_or_create_group(self, group_name):
    if len(group_name) == 0:
      raise Exception('Invalid group name')
    
    if group_name in self._group_cache:
      return self._group_cache[group_name]
      
    #check if exists
    groups = cachet.Groups(endpoint=self.ENDPOINT, api_token=self.API_TOKEN)
    params = {'name': group_name}
    data = json.loads(groups.get(params = params))
    
    if len(data['data']) > 0:
      id = data['data'][0]['id']
      self._group_cache[group_name] = id
      
      self.logger.debug('Group %s already exists with id %d', group_name, id)
      
      return id
    
    data = json.loads(groups.post(name=group_name))
    id = data['data']['id']
    self._group_cache[group_name] = id
    
    self.logger.debug('Create new group %s, group id is %d', group_name, id)
    
    return id
  
  def _get_or_create_component(self, group_id, component_name):
    if len(component_name) == 0:
      raise Exception('Invalid component name')
    
    components = cachet.Components(endpoint=self.ENDPOINT, api_token=self.API_TOKEN)

    #check if exists
    params = {'name': component_name, 'group_id': group_id}
    data = json.loads(components.get(params=params))
    if len(data['data']) > 0:
      id = data['data'][0]['id']
      self.logger.debug('Component %s already exists with id %d', component_name, id)
      
      return id

    data = json.loads(components.post(name=component_name, group_id=group_id, status=1))
    id = data['data']['id']
    self.logger.debug('Create new component %s, component id is %d', component_name, id)
    
    return id

  def _update_component(self, component_id, state, description):
    components = cachet.Components(endpoint=self.ENDPOINT, api_token=self.API_TOKEN)
    components.put(id=component_id, status=state, description=description)
    self.logger.debug('Update component #%d status to %d', component_id, state)
  
  def _update_component_exception(self, component_id, err):
    self._update_component(component_id, 4, str(err))
    
  def run(self):
    if len(self._probes) == 0:
      print('There is not configured probes!')
      exit(1)
    
    while True:
      for component_id, probe in self._probes.items():
        try:
          state, description = probe.check()
          self._update_component(component_id, state, description)
          
        except Exception as err:
          self.logger.exception('Catch %s', err)
          self._update_component_exception(component_id, err)
      
      #wait
      time.sleep(self.CHECK_INTERVAL)

class SpringBootProbe(object):
  def __init__(self, params):
    self.DEFAULT_TIMEOUT = 5
    self.check_url = params[0].strip()
    if len(self.check_url) == 0:
      raise Exception('Invalid URL')
  
  def __str__(self):
    return self.__class__.__name__ + ('(check_url=%s)' % self.check_url)
  
  def check(self):
    try:
      r = requests.get(self.check_url, timeout=self.DEFAULT_TIMEOUT)
      data = r.json()
      
      #calculate description and status
      description = ""
      all_up = True
      
      for key, value in data.items():
        if 'status' in value:
          status = value['status']
          if status != 'UP':
            all_up = False
          
          description += key+': '+status+'\n'
      core_status = data['status']
      if core_status != 'UP':
        all_up = False
      
      description = 'Core: '+core_status+'\n'+description
      
      return 1 if all_up else 3, description
      
    except requests.exceptions.RequestException as err:
      return 4, str(err)

      
CACHET_ENDPOINT = 'CACHET_ENDPOINT'
CACHET_API_TOKEN = 'CACHET_API_TOKEN'
AGENT_CONFIGURATION = 'AGENT_CONFIGURATION'      

def init_logger(args):
  if args.vvv:
    logging.basicConfig(level=logging.DEBUG)
  elif args.vv:
    logging.basicConfig()
    logging.getLogger('cachetAgent').setLevel(logging.DEBUG)
  elif args.v:
    logging.basicConfig()
    logging.getLogger('cachetAgent').setLevel(logging.INFO)
  
def process_params(args):
  def first_val(*args):
    for arg in args:
      if arg is not None:
        return arg

  params = {
    'endpoint': first_val(os.environ.get(CACHET_ENDPOINT), args.endpoint),
    'apiToken': first_val(os.environ.get(CACHET_API_TOKEN), args.api_token),
    'checkInterval': args.check_interval,
    'configFile': args.config_file,
    'configData': os.environ.get(AGENT_CONFIGURATION)
  }
  
  return params

def exit_on_error(msg, parser):
  print(msg, file=sys.stderr)
  print()
  parser.print_help()
  exit(1)
  
def main():
  parser = argparse.ArgumentParser(description='Cachet agent')
  parser.add_argument('--endpoint', help='Cachet API endpoint (or use {var} env variable)'.format(var=CACHET_ENDPOINT))
  parser.add_argument('--api-token', help='Cachet API tokent (or use {var} env variable)'.format(var=CACHET_API_TOKEN))
  parser.add_argument('--check-interval', help='Check interval [s]', default='60', type=int)
  parser.add_argument('--config-file', help='Probes config file (or use {var} multiline env variable)'.format(var=AGENT_CONFIGURATION), default='agent.conf')
  parser.add_argument('-v', help='Show logs', action='store_const', const=1)
  parser.add_argument('-vv', help='Show more logs', action='store_const', const=1)
  parser.add_argument('-vvv', help='Be chatty', action='store_const', const=1)
  args = parser.parse_args()
  
  init_logger(args)
  params = process_params(args)
  if params['endpoint'] is None:
    exit_on_error('Cachet API endpoint is not set!', parser)
  if params['apiToken'] is None:
    exit_on_error('Cachet API token is not set!', parser)
  
  agent = CachetAgent(endpoint=params['endpoint'], api_token=params['apiToken'], check_interval=params['checkInterval'], conf_file=params['configFile'], conf_data=params['configData'])
  agent.run()
  
if __name__ == '__main__':
  main()