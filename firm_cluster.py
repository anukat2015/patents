# name matching using locally sensitive hashing (simhash)
# this is python3
# these are mostly idempotent

from itertools import chain, repeat
from collections import defaultdict
from math import ceil

import sqlite3
import numpy as np
import networkx as nx
try:
    from distance.cdistance import levenshtein
except:
    from distance import levenshtein

from name_standardize import name_standardize_weak, name_standardize_strong
import simhash as sh

#
# globals
#

db_fname = 'store/patents.db'

#
# data processing routines
#

# white magic
def autodb(fname,has_con=True,has_cur=True):
    def wrap(f):
        if has_con or has_cur:
            def f1(*args,**kwargs):
                con = sqlite3.connect(fname)
                try:
                    if has_cur:
                        cur = con.cursor()
                        if has_con:
                            ret = f(con=con,cur=cur,*args[2:],**kwargs)
                        else:
                            ret = f(cur=cur,*args[1:],**kwargs)
                    else:
                        ret = f(con=con,*args,**kwargs)
                finally:
                    con.close()
                return ret
            return f1
        else:
            return f
    return wrap

@autodb(db_fname)
def generate_names(con,cur):
    # standardize compustat names
    cur.execute('drop table if exists compustat_std')
    cur.execute('create table compustat_std (gvkey int, year int, namestd text)')
    ret = cur.execute('select gvkey,year,name from compustat')
    cur.executemany('insert into compustat_std values (?,?,?)',[(gvkey,year,name_standardize_weak(owner)) for (gvkey,year,owner) in ret])

    # standardize patent names
    cur.execute('drop table if exists patent_std')
    cur.execute('create table patent_std (patnum int, namestd int)')
    ret = cur.execute('select patnum,owner from patent_use')
    cur.executemany('insert into patent_std values (?,?)',[(patnum,name_standardize_weak(owner)) for (patnum,owner) in ret])

    # standardize assignment names
    cur.execute('drop table if exists assignment_std')
    cur.execute('create table assignment_std (assignid int, assigneestd int, assignorstd)')
    ret = cur.execute('select assignid,assignor,assignee from assignment_use')
    cur.executemany('insert into assignment_std values (?,?,?)',[(assignid,name_standardize_weak(assignor),name_standardize_weak(assignee)) for (assignid,assignor,assignee) in ret])

    # store unique names
    cur.execute('drop table if exists owner')
    cur.execute('create table owner (ownerid integer primary key asc, name text)')
    cur.execute("""insert into owner(name) select namestd from compustat_std
                   union select namestd from patent_std
                   union select assigneestd from assignment_std
                   union select assignorstd from assignment_std""")

    # map back into compustat
    cur.execute('drop table if exists compustat_owner')
    cur.execute('create table compustat_owner (gvkey int, year int, ownerid int)')
    cur.execute('insert into compustat_owner select gvkey,year,ownerid from compustat_std join owner on compustat_std.namestd=owner.name')

    # map back into patent
    cur.execute('drop table if exists patent_owner')
    cur.execute('create table patent_owner (patnum int, ownerid int)')
    cur.execute('insert into patent_owner select patnum,ownerid from patent_std join owner on patent_std.namestd=owner.name')

    # map back into assignments
    cur.execute('drop table if exists assignment_owner')
    cur.execute('create table assignment_owner (assignid int, assigneeid int, assignorid int)')
    cur.execute("""insert into assignment_owner select assignid,assignee_owner.ownerid,assignor_owner.ownerid from assignment_std
                   join owner as assignee_owner on assignment_std.assigneestd=assignee_owner.name
                   join owner as assignor_owner on assignment_std.assignorstd=assignor_owner.name""")

    con.commit()

# k = 8, thresh = 4 works well
@autodb(db_fname)
def owner_cluster(con,cur,nitem=None,reverse=True,nshingle=2,store=False,**kwargs):
    c = sh.Cluster(**kwargs)

    cmd = 'select ownerid,name from owner'
    if reverse:
        cmd += ' order by rowid desc'
    if nitem:
        cmd += ' limit %i' % nitem

    name_dict = {}
    for (i,(ownerid,name)) in enumerate(cur.execute(cmd)):
        words = name.split()
        shings = list(sh.shingle(name,nshingle))

        features = shings + words
        weights = list(np.linspace(1.0,0.0,len(shings))) + list(np.linspace(1.0,0.0,len(words)))

        c.add(features,weights=weights,label=ownerid)
        name_dict[ownerid] = name

        if i%10000 == 0:
            print(i)

    ipairs = c.unions
    npairs = [(name_dict[i1],name_dict[i2]) for (i1,i2) in ipairs]
    print('Found %i pairs' % len(ipairs))

    if store:
        cur.execute('drop table if exists pair')
        cur.execute('create table pair (ownerid1 int, ownerid2 int, name1 text, name2 text)')
        cur.executemany('insert into pair values (?,?,?,?)',[(o1,o2,n1,n2) for ((o1,o2),(n1,n2)) in zip(ipairs,npairs)])
        con.commit()
    else:
        return (ipairs,npairs)

# compute distances on owners in same cluster
@autodb(db_fname)
def find_components(con,cur,thresh=0.85,store=False):
    cmd = 'select * from pair'

    def dmetr(name1,name2):
        maxlen = max(len(name1),len(name2))
        ldist = levenshtein(name1,name2,max_dist=int(ceil(maxlen*(1.0-thresh))))
        return (1.0 - float(ldist)/maxlen) if (ldist != -1 and maxlen != 0) else 0.0

    dists = []
    close = []
    name_dict = {}
    name_std = {}

    for (o1,o2,n1,n2) in cur.execute(cmd):
        if o1 not in name_dict:
            n1s = name_standardize_strong(n1)
            name_dict[o1] = n1
            name_std[o1] = n1s
        else:
            n1s = name_std[o1]
        if o2 not in name_dict:
            n2s = name_standardize_strong(n2)
            name_dict[o2] = n2
            name_std[o2] = n2s
        else:
            n2s = name_std[o2]

        d = dmetr(n1s,n2s)

        dists.append((o1,o2,d))
        if d > thresh:
            close.append((o1,o2))

    G = nx.Graph()
    G.add_edges_from(close)
    comps = sorted(nx.connected_components(G),key=len,reverse=True)

    if store:
        cur.execute('drop table if exists component')
        cur.execute('create table component (compid int, ownerid int)')
        cur.executemany('insert into component values (?,?)',chain(*[zip(repeat(cid),comp) for (cid,comp) in enumerate(comps)]))
        con.commit()
    else:
        comp_names = [[name_std[id] for id in ids] for ids in comps]
        return comp_names

# must be less than 1000000 components
@autodb(db_fname)
def merge_components(con,cur):
    # match owners to firms
    cur.execute('drop table if exists owner_firm')
    cur.execute('create table owner_firm (ownerid int, firm_num int)')
    cur.execute('insert into owner_firm select ownerid,compid+1000000 from owner left join component using(ownerid)')
    cur.execute('update owner_firm set firm_num=ownerid where firm_num is null')

    cur.execute('drop table if exists compustat_merge')
    cur.execute("""create table compustat_merge as select compustat.*,compustat_owner.ownerid,owner_firm.firm_num
                   from compustat left join compustat_owner using(gvkey,year)
                   left join owner_firm using(ownerid)""")

    cur.execute('drop table if exists patent_merge')
    cur.execute("""create table patent_merge as select patent_use.*,patent_owner.ownerid,owner_firm.firm_num
                   from patent_use left join patent_owner using(patnum)
                   left join owner_firm using(ownerid)""")

    cur.execute('drop table if exists assignment_merge')
    cur.execute("""create table assignment_merge as select assignment_use.*,assignment_owner.assigneeid,assignment_owner.assignorid,assignee_firm.firm_num as dest_fn,assignor_firm.firm_num as source_fn
                   from assignment_use left join assignment_owner on assignment_use.assignid=assignment_owner.assignid
                   left join owner_firm as assignee_firm on assignment_owner.assigneeid=assignee_firm.ownerid
                   left join owner_firm as assignor_firm on assignment_owner.assignorid=assignor_firm.ownerid""")

    # aggregate to yearly
    cur.execute('drop table if exists patent_basic')
    cur.execute('create table patent_basic (patnum integer primary key, firm_num int, fileyear int, grantyear int, state text, country text, classone int, classtwo int)')
    cur.execute('insert into patent_basic select patnum,firm_num,substr(filedate,1,4),substr(grantdate,1,4),state,country,classone,classtwo from patent_merge')
    cur.execute('create unique index patent_basic_idx on patent_basic(patnum)')

    cur.execute('drop table if exists assignment_info')
    cur.execute('create table assignment_info (assignid integer primary key, patnum int, source_fn int, dest_fn int, execyear int, recyear int, state text, country text)')
    cur.execute('insert into assignment_info select assignid,patnum,source_fn,dest_fn,substr(execdate,1,4),substr(recdate,1,4),assignee_state,assignee_country from assignment_merge')

    cur.execute('drop table if exists assignment_bulk')
    cur.execute('create table assignment_bulk (source_fn int, dest_fn int, execyear int, ntrans int)')
    cur.execute('insert into assignment_bulk select source_fn,dest_fn,execyear,count(*) from assignment_info group by source_fn,dest_fn,execyear')

    con.commit()

@autodb(db_fname)
def get_names(con,cur,olist=[]):
    return cur.execute('select * from owner where ownerid in (%s)' % ','.join([str(o) for o in olist])).fetchall()

@autodb(db_fname)
def get_component(con,cur,compid=0):
    owners = [x for (x,) in cur.execute('select ownerid from component where compid=?',(compid,)).fetchall()]
    return cur.execute('select * from owner where ownerid in (%s)' % ','.join([str(o) for o in owners])).fetchall()
