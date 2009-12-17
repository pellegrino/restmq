# coding: utf-8

import types
import simplejson
from twisted.internet import defer

POLICY_BROADCAST = 1
POLICY_ROUNDROBIN = 2

class RedisOperations:
    """
    add element to the queue:
        - increments a UUID record 
        - store the object using a key as <queuename>:uuid
        - push this key into a list named <queuename>:queue
        - push this list name into the general QUEUESET
    get element from queue:
        - pop a key from the list
        - get and return, along with its key

    del element from the queue:
        - tricky part. there must be a queue_get() before. The object is out of the queue already. delete it.
        
    - TODO: the object may have an expiration instead of straight deletion
    - TODO: RPOPLPUSH can be used to put it in another queue as a backlog
    - TODO: persistence management (on/off/status)
    """

    def __init__(self, redis):
        self.redis = redis
        self.policies = {
            "broadcast": POLICY_BROADCAST,
            "roundrobin": POLICY_ROUNDROBIN,
        }
        self.inverted_policies = dict([[v, k] for k, v in self.policies.items()])
        self.QUEUESET = 'QUEUESET' # the set which holds all queues

    def normalize(self, item):
        if isinstance(item, types.StringType):
            return item
        elif isinstance(item, types.UnicodeType):
            try:
                return item.encode("utf-8")
            except:
                raise ValueError("strings must be utf-8")
        else:
            raise ValueError("data must be utf-8")
        
    @defer.inlineCallbacks
    def queue_add(self, queue, value):
        queue, value = self.normalize(queue), self.normalize(value)

        uuid = yield self.redis.incr("%s:UUID" % queue)
        key = '%s:%d' % (queue, uuid)
        res = yield self.redis.set(key, value)
        
        lkey = '%s:queue' % queue
        
        if uuid == 1: # TODO: use ismember()
            # either by checking uuid or by ismember, this is where you must know if the queue is a new one.
            # add to queues set
            res = yield self.redis.sadd(self.QUEUESET, lkey)
            #print "set add: %s" % res

            # add default queue policy, for now just enforce_take is set
            #yield self.queue_policy_set(queue, "broadcast")
            #qpkey = "%s:queuepolicy" % (queue)
            #defaultqp = {'enforce_take':False, 'broadcast':True}
            #res = yield self.redis.set(qpkey, simplejson.dumps(defaultqp).encode('utf-8'))


        res = yield self.redis.push(lkey, key)
        defer.returnValue(key)

    @defer.inlineCallbacks
    def queue_get(self, queue, softget=False): 
        """
            GET can be either soft or hard. 
            SOFTGET means that the object is not POP'ed from its queue list. It only gets a refcounter which is incremente for each GET
            HARDGET is the default behaviour. It POPs the key from its queue list.
            NoSQL dbs as mongodb would have other ways to deal with it. May be an interesting port.
            The reasoning behing refcounters is that they are important in some job scheduler patterns.
            To really cleanup the queue, one would have to issue a DEL after a hard GET.
        """
        policy = None
        queue = self.normalize(queue)
        lkey = '%s:queue' % queue
        if softget == False:
            okey = yield self.redis.pop(lkey)
        else:
            okey = yield self.redis.lindex(lkey, "0")

        if okey == None:
            defer.returnValue((None, None))
            return

        #val = yield self.redis.get(okey.encode('utf-8'))
        qpkey = "%s:queuepolicy" % queue
        (policy, val) = yield self.redis.mget(qpkey, okey.encode('utf-8'))
        c=0
        if softget == True:
            c = yield self.redis.incr('%s:refcount' % okey.encode('utf-8'))

        defer.returnValue((policy or POLICY_BROADCAST, {'key':okey, 'value':val, 'count':c}))

    
    @defer.inlineCallbacks
    def queue_del(self, queue, okey):
        """
            DELetes an element from redis (not from the queue).
            Its important to make sure a GET was issued before a DEL. Its a kinda hard to guess the direct object key w/o a GET tho.
            the return value contains the key and value, which is a del return code from Redis. > 1 success and N keys where deleted, 0 == failure
        """
        queue, okey = self.normalize(queue), self.normalize(okey)
        val = yield self.redis.delete(okey)
        defer.returnValue({'key':okey, 'value':val})

    @defer.inlineCallbacks
    def queue_len(self, queue):
        lkey = '%s:queue' % self.normalize(queue)
        ll = yield self.redis.llen(lkey)
        defer.returnValue({'len': ll})

    @defer.inlineCallbacks
    def queue_all(self):
        sm = yield self.redis.smembers(self.QUEUESET)
        defer.returnValue({'queues': sm})
    
    @defer.inlineCallbacks
    def queue_getdel(self, queue):
        policy = None
        queue = self.normalize(queue)
        lkey = '%s:queue' % queue

        okey = yield self.redis.pop(lkey) # take from queue's list
        if okey == None:
            defer.returnValue((None, False))
            return
        okey = self.normalize(okey)
        nkey = '%s:lock' % okey
        ren = yield self.redis.rename(okey, nkey) # rename key

        if ren == None:
            defer.returnValue((None,None))
            return

        qpkey = "%s:queuepolicy" % queue
        (policy, val) = yield self.redis.mget(qpkey, nkey)
        delk = yield self.redis.delete(nkey)
        if delk == 0:
            defer.returnValue((None, None))
        defer.returnValue((policy, {'key':okey, 'value':val}))

    @defer.inlineCallbacks
    def queue_policy_set(self, queue, policy):
        queue, policy = self.normalize(queue), self.normalize(policy)
        if policy in ("broadcast", "roundrobin"):
            policy_id = self.policies[policy]
            qpkey = "%s:queuepolicy" % (queue)
            res = yield self.redis.set(qpkey, policy_id)
            defer.returnValue({'queue': queue, 'response': res})
        else:
            defer.returnValue({'queue': queue, 'response': ValueError("invalid policy: %s" % repr(policy))})

    @defer.inlineCallbacks
    def queue_policy_get(self, queue):
        queue = self.normalize(queue)
        qpkey = "%s:queuepolicy" % (queue)
        val = yield self.redis.get(qpkey)
        defer.returnValue({'queue':queue, 'value': self.inverted_policies.get(val, "unknown")})

    @defer.inlineCallbacks
    def queue_tail(self, queue, keyno=10): 
        """
            TAIL follows on GET, but returns keyno keys instead of only one key.
            keyno could be a LLEN function over the queue list, but it lends almost the same effect.
            LRANGE could too fetch the latest keys, even if there was less than keyno keys. MGET could be used too.
            TODO: does DELete belongs here ?
            return is a tuple (policy, returnvalues[])
        """
        policy = None
        queue = self.normalize(queue)
        lkey = '%s:queue' % queue
        keys=[]
        multivalue = []
        for a in range (keyno):
            t = yield self.redis.pop(lkey)
            if t != None: 
                k = t.encode('utf-8')
                keys.append(k)
                v = yield self.redis.get(k) # queue_getdel would keep the db clean
                multivalue.append({'key': k, 'value':v.encode('utf-8')})
        
        if len(keys) == 0:
            defer.returnValue((None, None))
            return

        qpkey = "%s:queuepolicy" % queue
        policy = yield self.redis.get(qpkey)
        defer.returnValue((policy or POLICY_BROADCAST, multivalue))

    @defer.inlineCallbacks
    def queue_count_elements(self, queue):
        # it hurts, uses too much space :(
        # but it's necessary to evaluate how many objects still undeleted on redis.
        lkey = '%s*' % self.normalize(queue)
        ll = yield self.redis.keys(lkey)
        defer.returnValue({"objects":len(ll)})

    @defer.inlineCallbacks
    def queue_last_items(self, queue, count=10): 
        """
            returns a list with the last count items in the queue
        """
        queue = self.normalize(queue)
        lkey = '%s:queue' % queue
        multivalue = yield self.redis.lrange(lkey, 0, count-1)

        defer.returnValue( multivalue)


