import copy
import json
import time
import sys,os,shutil
import warnings
import tempfile

from . import debug,log
from . import utils
from .rclone import Rclone
from .dicttable import DictTable

_TEST_AVOID_RELIST = False

class LockedRemoteError(ValueError):
    pass

class SyncRClone:
    def __init__(self,config,break_lock=None):
        """
        Main sync object. If break_lock is not None, will *just* break the
        locks
        """
        self.now = time.strftime("%Y-%m-%dT%H%M%S", time.localtime())
        self.now_compact = self.now.replace('-','')
        
        self.config = config
        self.config.now = self.now # Set it there to be used elsewhere
        self.rclone = Rclone(self.config)
        
        self.rclone.run_shell(pre=True)

        if break_lock:
            if self.config.dry_run:
                log('DRY RUN lock break')
                return
            self.rclone.lock(breaklock=True,remote=break_lock)
            return

        # Get file lists
        log('')
        log(f"Refreshing file list on A '{config.remoteA}'")
        self.currA,self.prevA = self.rclone.file_list(remote='A')   
        log(utils.file_summary(self.currA))
        
        log(f"Refreshing file list on B '{config.remoteB}'")
        self.currB,self.prevB = self.rclone.file_list(remote='B')
        log(utils.file_summary(self.currB))

        self.check_lock()
        if self.config.set_lock and not config.dry_run: # no lock for dry-run since we won't change anything
            self.rclone.lock()
        
        # Store the original "curr" list as the prev list for speeding
        # up the hashes. Also used to tag checking.
        # This makes a copy but keeps the items
        self.currA0 = self.currA.copy()
        self.currB0 = self.currB.copy()
        
        self.remove_common_files()
        self.process_non_common() # builds new,del,tag,backup,trans,move lists
        
        self.echo_queues('Initial')
        
        # Track moves from new and del lists. Adds to moves list()
        self.track_moves('A')
        self.track_moves('B') 
        
        self.echo_queues('After tracking moves')
    
        # Apply moves and transfers from the new and tag lists.
        # After this, we only care about del, backup, and move lists
        self.process_new_tags('A') 
        self.process_new_tags('B')
        
        self.echo_queues('After processing new and tags')
        
        if config.dry_run:
            self.summarize(dry=True)
            self.rclone.run_shell(pre=False) # has a --dry-run catch
            self.dump_logs()
            return
        
        # Perform deletes, backups, and moves
        if config.interactive:
            self.summarize(dry=None) 
            # I may add a timeout in the future but the  easiest method, 
            # select.select, will preclude (eventual?) windows support
            cont = input('Would you like to continue? Y/[N]: ')
            if not cont.lower().startswith('y'):
                self.rclone.run_shell(pre=False) # TODO: Consider if this should be here. Or should we always shell?
                sys.exit()
        else:
            self.summarize(dry=False)  
        
        # Do actions. Clear the backup list if not using rather than keep around.
        # This way, we do not accidentally transfer it if not backed up
        if not config.backup: # Delete in place though I don't think it matters
            del self.backupA[:]
            del self.backupB[:]
        
        log('');log('Performing Actions on A')
        self.rclone.delete_backup_move('A',self.delA,self.backupA,self.movesA)
        if config.backup and (self.delA or self.backupA):
            log(f"""Backups for A stored in '{self.rclone.backup_path["A"]}'""")
        
        log('');log('Performing Actions on B')
        self.rclone.delete_backup_move('B',self.delB,self.backupB,self.movesB)
        if config.backup and (self.delB or self.backupB):
            log(f"""Backups for B stored in '{self.rclone.backup_path["B"]}'""")
        
        # Add the backed up files to be transfered too. This way the backups 
        # are on *both* systems. Only del and backup lists add to the backup.
        # Keep as part of the single transfer
        if config.backup and config.sync_backups:
            self.transA2B.extend(os.path.join(self.rclone.backup_path0['A'],f) 
                for f in self.delA + self.backupA)
            self.transB2A.extend(os.path.join(self.rclone.backup_path0['B'],f) 
                for f in self.delB + self.backupB)
            
        # Perform final transfers
        sumA = utils.file_summary([self.currA.query_one(Path=f) for f in self.transA2B])
        log('');log(f'A >>> B {sumA}')
        self.rclone.transfer('A2B',self.transA2B)
        
        sumB = utils.file_summary([self.currB.query_one(Path=f) for f in self.transB2A])
        log('');log(f'A <<< B {sumB}')
        self.rclone.transfer('B2A',self.transB2A)

        # Update lists if needed
        log('')
        if self.config.avoid_relist:
            log('Apply changes to file lists instead of refreshing')
            new_listA,new_listB = self.avoid_relist()
        else:
            if self.delA or self.backupA or self.movesA or self.transB2A:
                log('Refreshing file list on A')
                new_listA,_ = self.rclone.file_list(remote='A',prev_list=self.currA0)
                log(utils.file_summary(new_listA))
            else:
                log('No need to refresh file list on A')
                new_listA = self.currA0
        
            if self.delB or self.backupB or self.movesB or self.transA2B:
                log('Refreshing file list on B')
                new_listB,_ = self.rclone.file_list(remote='B',prev_list=self.currB0)
                log(utils.file_summary(new_listB))
            else:
                log('No need to refresh file list on B')
                new_listB = self.currB0 
            
        if config.cleanup_empty_dirsA or (config.cleanup_empty_dirsA is None and\
                                          self.rclone.empty_dir_support('A')): 
            emptyA = {os.path.dirname(f['Path']) for f in self.currA0} \
                   - {os.path.dirname(f['Path']) for f in new_listA}
            self.rclone.rmdirs('A',emptyA)
        
        if config.cleanup_empty_dirsB or (config.cleanup_empty_dirsB is None and\
                                          self.rclone.empty_dir_support('B')): 
            emptyB = {os.path.dirname(f['Path']) for f in self.currB0} \
                   - {os.path.dirname(f['Path']) for f in new_listB}
            self.rclone.rmdirs('B',emptyB)
        
        
        ######## For testing only
        if _TEST_AVOID_RELIST:
            re_listA,re_listB = self.avoid_relist()
            with open('relists.json','wt') as fout:
                json.dump({'A':list(new_listA),'B':list(new_listB),
                           'rA':list(re_listA),'rB':list(re_listB)},fout)
        ########
               
        log('Uploading filelists')
        self.rclone.push_file_list(new_listA,remote='A')
        self.rclone.push_file_list(new_listB,remote='B')

        if self.config.set_lock: # There shouldn't be a lock since we didn't set it so save the rclone call
            self.rclone.lock(breaklock=True)

        self.rclone.run_shell(pre=False)            
        self.dump_logs()

    
    def dump_logs(self):
        if not self.config.local_log_dest and not self.config.log_dest:
            log('Logs are not being saved')
            return
        
        logname = f"{self.config.name}_{self.now}.log"
        
        # log these before dumping
        if self.config.local_log_dest:
            log(f"Logs will be saved locally to '{os.path.join(self.config.local_log_dest,logname)}'")
        if self.config.log_dest:
            log(f"Logs will be saved on remotes to '{os.path.join(self.config.log_dest,logname)}'")
        
        tfile =  os.path.join(self.rclone.tmpdir,'log')
        log.dump(tfile)
        
        if self.config.local_log_dest:
            dest = os.path.join(self.config.local_log_dest,logname)
            try: os.makedirs(os.path.dirname(dest))
            except OSError: pass
            shutil.copy2(tfile,dest)
        
        if self.config.log_dest:
            self.rclone.copylog('A',tfile,os.path.join(self.config.log_dest,logname))
            self.rclone.copylog('B',tfile,os.path.join(self.config.log_dest,logname))
                
    def summarize(self,dry=False):
        """
        dry can be True, False, or None where None is to show the planned
        """
        if dry is True:
            tt = '(DRY RUN) '
            log(tt.strip())
        elif dry is False:
            tt = ''
        elif dry is None:
            tt = '(PLANNED) '
        else:
            raise ValueError() # Just in case I screw up later
        
        attr_names = {'del':'Delete (with{} backup)'.format('out' if not self.config.backup else ''),
                      'backup':'Backup'}
        if not self.config.backup:
            attr_names['backup'] = "Will overwrite (w/o backup)"
            
        for AB in 'AB':
            log('')
            log(f"Actions queued on {AB}:")
            for attr in ['del','backup','moves']:
                for file in getattr(self,f'{attr}{AB}'):
                    if attr == 'moves':
                        log(f"{tt}Move on {AB}: '{file[0]}' --> '{file[1]}'")
                    else:
                        log(f"{tt}{attr_names.get(attr,attr)} on {AB}: '{file}'")
        if dry is False:
            return
        
        sumA = utils.file_summary([self.currA.query_one(Path=f) for f in self.transA2B])
        sumB = utils.file_summary([self.currB.query_one(Path=f) for f in self.transB2A])
        
        log('');log(f'{tt}A >>> B {sumA}')
        for file in self.transA2B:
            log(f"{tt}Transfer A >>> B: '{file}'")
        log('');log(f'{tt}A <<< B {sumB}')
        for file in self.transB2A:
            log(f"{tt}Transfer A <<< B: '{file}'")
            
    def echo_queues(self,descr=''):
        debug(f'Printing Queueus {descr}')
        for attr in ['new','del','tag','backup','trans','moves']:
            for AB in 'AB':
                BA = 'B' if AB == 'A' else 'A'
                if attr == 'trans':
                    pa = f'{attr}{AB}2{BA}'
                else:
                    pa = f'{attr}{AB}'
                debug('   ',pa,getattr(self,pa))
    
    def remove_common_files(self):
        """
        Removes files common in the curr list from the curr lists and,
        if present, the prev lists
        """
        config = self.config
        commonPaths = set(file['Path'] for file in self.currA)
        commonPaths.intersection_update(file['Path'] for file in self.currB)

        delpaths = set()
        for path in commonPaths:
            q = {'Path':path}
            # We KNOW they exists for both
            fileA,fileB = self.currA[q],self.currB[q]
            if not self.compare(fileA,fileB):
                continue
            delpaths.add(path)
            
        for attr in ['currA','prevA','currB','prevB']:
            new = DictTable([f for f in getattr(self,attr) if f['Path'] not in delpaths],
                           fixed_attributes=['Path','Size','mtime'])
            setattr(self,attr,new)
        
        debug(f'Found {len(commonPaths)} common paths with {len(delpaths)} matching files')

    def process_non_common(self):
        """
        Create action lists (some need more processing) and then populate 
        with all remaining files
        """
        config = self.config    
        
        # These are for classifying only. They are *later* translated
        # into actions
        self.newA,self.newB = list(),list() # Will be moved to transfer
        self.delA,self.delB = list(),list() # Action but may be modified by move tracking later
        self.tagA,self.tagB = list(),list() # Will be tagged (moved) then transfer
        
        # These will not need be modified further. 
        self.backupA,self.backupB = list(),list()
        self.transA2B,self.transB2A = list(),list()
        self.movesA,self.movesB = list(),list() # Not used here but created for use elsewhere
        
        # All paths. Note that common paths with equal files have been cut
        allPaths = set(file['Path'] for file in self.currA)
        allPaths.update(file['Path'] for file in self.currB)

        # NOTE: Final actions will be done in the following order
        # * Delete
        # * Backup -- Always assign but don't perform if --no-backup
        # * Move (including tag)
        # * Transfer
        log('')
        for path in allPaths:
            fileA = self.currA[{'Path':path}]
            fileB = self.currB[{'Path':path}]
            fileBp = self.prevB[{'Path':path}]
            fileAp = self.prevA[{'Path':path}]

            if fileA is None: # fileB *must* exist
                if not fileBp:
                    debug(f"File '{path}' is new on B")
                    self.newB.append(path) # B is new
                elif self.compare(fileB,fileBp):
                    debug(f"File '{path}' deleted on A")
                    self.delB.append(path) # B must have been deleted on A
                else:
                    log(f"DELETE CONFLICT: File '{path}' deleted on A but modified on B. Transfering")
                    self.transB2A.append(path)
                continue
                    
            if fileB is None: # fileA *must* exist
                if not fileAp:
                    debug(f"File '{path}' is new on A")
                    self.newA.append(path) # A is new
                elif self.compare(fileA,fileAp):
                    debug(f"File '{path}' deleted on A")
                    self.delA.append(path) # A must have been deleted on B
                else:
                    log(f"DELETE CONFLICT: File '{path}' deleted on B but modified on A. Transfering")
                    self.transA2B.append(path)
                continue
                  
            # We *know* they do not agree since this common ones were removed.
            # Now must decide if this is a conflict or just one was modified
            compA = self.compare(fileA,fileAp)
            compB = self.compare(fileB,fileBp)
            
            debug(f"Resolving:\n{json.dumps({'A':fileA,'Ap':fileAp,'B':fileB,'Bp':fileB},indent=1)}")
            
            if compA and compB:
                # This really shouldn't happen but if it does, just move on to
                # conflict resolution
                debug(f"'{path}': Both A and B compare to prev but do not agree. This is unexpected.")
            elif not compA and not compB:
                # Do nothing but note it. Deal with conflict below
                debug(f"'{path}': Neither compare. Both modified or both new")
            elif compA and not compB: # B is modified, A is not
                debug(f"'{path}': Modified on B only")
                self.transB2A.append(path)
                self.backupA.append(path)  
                continue
            elif not compA and compB: # A is modified, B is not
                debug(f"'{path}': Modified on A only")
                self.transA2B.append(path)
                self.backupB.append(path)  
                continue
            
            # They conflict! Handle it.
            mA,mB = fileA.get('mtime',None),fileB.get('mtime',None)
            sA,sB = fileA['Size'],fileB['Size']

            txtA =  utils.unix2iso(mA) if mA else '<< not found >>' 
            txtA += ' ({:0.2g} {:s})'.format(*utils.bytes2human(sA,short=True))
            txtB =  utils.unix2iso(mB) if mB else '<< not found >>' 
            txtB += ' ({:0.2g} {:s})'.format(*utils.bytes2human(sB,short=True))

            if config.conflict_mode not in {'newer','older','newer_tag'}:
                mA,mB = sA,sB # Reset m(AB) to s(AB)
            
            if not mA or not mB: # Either never set for non-mtime compare or no mtime listed
                warnings.warn('No mtime found. Resorting to size')
                mA,mB = sA,sB # Reset m(AB) to s(AB)
                
            log(f"CONFLICT '{path}'")
            log(f"    A: {txtA}")
            log(f"    B: {txtB}")
            
            txt = f"    Resolving with mode '{config.conflict_mode}'"
            
            if config.tag_conflict:        
                tag_or_backupA = self.tagA 
                tag_or_backupB = self.tagB 
                txt += ' (tagging other)'
            else:
                tag_or_backupA = self.backupA 
                tag_or_backupB = self.backupB
            
            if config.conflict_mode == 'A':
                self.transA2B.append(path)
                tag_or_backupB.append(path)
            elif config.conflict_mode == 'B':
                self.transB2A.append(path)
                tag_or_backupA.append(path)
            elif config.conflict_mode == 'tag':
                self.tagA.append(path) # Tags will *later* be added to transfer queue
                self.tagB.append(path)
            elif not mA or not mB or mA == mB:
                txt = f"    Cannot resolve conflict with '{config.conflict_mode}'. Reverting to tagging both"
                self.tagA.append(path) # Tags will *later* be added to transfer queue
                self.tagB.append(path)
            elif mA > mB:
                if config.conflict_mode in ('newer','larger'):
                    self.transA2B.append(path)
                    tag_or_backupB.append(path)
                    txt += '(keep A)'
                elif config.conflict_mode in ('older','smaller'):
                    self.transB2A.append(path)
                    tag_or_backupA.append(path)
                    txt += '(keep B)'
            elif mA < mB:
                if config.conflict_mode in ('newer','larger'):
                    self.transB2A.append(path)
                    tag_or_backupA.append(path)
                    txt += '(keep B)'
                elif config.conflict_mode in ('older','smaller'):
                    self.transA2B.append(path)
                    tag_or_backupB.append(path)
                    txt += '(keep A)'
            else:# else: won't happen since we validated conflict modes
                raise ValueError('Comparison Failed. Please report to developer') # Should not be here
            
            log(txt)
                                
    def track_moves(self,remote):
        config = self.config
        AB = remote
        BA = list(set('AB')- set(AB))[0]
        remote = {'A':config.remoteA,'B':config.remoteB}[remote]      
        
        rename_attrib = getattr(config,f'renames{AB}')
        if not rename_attrib:
            return
        
        # A file move is *only* tracked if it marked
        # (1) Marked as new
        # (2) Can be matched via renames(A/B) to a remaining file in prev
        # (3) The same file is marked for deletion (No need to check anything 
        #     since a file is *only* deleted if it was present in the last sync
        #     and unmodified. So it is safe to move it
        
        new = getattr(self,f'new{AB}') # on remote -- list
        
        curr = getattr(self,f'curr{AB}') # on remote -- DictTable
        prev = getattr(self,f'prev{AB}') # on remote -- DictTable
        
        if not new or not curr or not prev:
            debug('No need to move track')
            return
        
        delOther = getattr(self,f'del{BA}') # On OTHER side -- list
        moveOther = getattr(self,f'moves{BA}') # on OTHER side - list
        
        # ALWAYS query size. This will cut out a lot of potential matches which
        # is good since hash and mtime need to search. (We search on hash in case the
        # do not always share a common one)
        for path in new[:]: # (1) Marked as new. Make sure to iterate a copy
            debug(f"Looking for moves on {AB}: '{path}'")
            currfile = curr[{'Path':path}]
        
            prevfiles = list(prev.query({'Size':currfile['Size']}))
            
            # The mtime and hash comparisons are in loops but this is not too bad
            # since the size check *greatly* reduces the size of the loops
            
            # Compare time with tol.
            if rename_attrib in ['mtime']:
                prevfiles = [f for f in prevfiles if abs(f['mtime'] - currfile['mtime']) < config.dt]
            
            # Compare hashes one-by-one in case they're not all the same types
            if rename_attrib == 'hash': 
                _prevfiles = []
                for prevfile in prevfiles:
                    hcurr = currfile.get('Hashes',{})
                    hprev = prevfile.get('Hashes',{})
            
                    # Just because there are common hashes, does *not* mean they are
                    # all populated. e.g, it could be a blank string.
                    # It is also possible for there to not be common hashes if the lists
                    # were not refreshed
                    common = {k for k,v in hcurr.items() if v.strip()}.intersection(\
                              k for k,v in hprev.items() if v.strip())
                    if common and all(hcurr[k] == hprev[k] for k in common):
                        _prevfiles.append(prevfile)
                prevfiles = _prevfiles # rename with the new lists
            
            if not prevfiles:
                debug(f"No matches for '{path}' on {AB}")
                continue
                
            if len(prevfiles) > 1:
                log(f"Too many possible previous files for '{path}' on {AB}")
                for f in prevfiles:
                    log(f"   '{f['Path']}'")
                continue
            prevpath = prevfiles[0]['Path'] # (2) Previous file
            
            if prevpath not in delOther:
                debug(f"File '{path}' moved from '{prevpath}' on {AB} but modified")
                continue
            
            # Move it instead
            new.remove(path)
            delOther.remove(prevpath)
            moveOther.append((prevpath,path))
            debug(f"Move found: on {BA}: '{prevpath}' --> '{path}'")
    
    def process_new_tags(self,remote):
        """Process new into transfers and tags into moves"""
        config = self.config
        AB = remote
        BA = list(set('AB')- set(AB))[0]
        remote = {'A':config.remoteA,'B':config.remoteB}[remote]
        
        new = getattr(self,f'new{AB}')
        tag = getattr(self,f'tag{AB}')
        trans = getattr(self,f'trans{AB}2{BA}')
        moves =  getattr(self,f'moves{AB}')

        for file in tag:
            root,ext = os.path.splitext(file)
            dest = f'{root}.{self.now_compact}.{AB}{ext}'
            moves.append((file,dest))
            debug(f"Added '{file}' --> '{dest}'")
            
            trans.append(dest) # moves happen before transfers!
        
        trans.extend(new)
    
    def check_lock(self,remote='both',error=True):
       # import ipdb;ipdb.set_trace()
        AB = remote
        if remote == 'both':
            locks = [] # Get BOTH before errors
            locks.extend(self.check_lock(remote='A',error=False))
            locks.extend(self.check_lock(remote='B',error=False))
        else:        
            curr = getattr(self,f'curr{AB}')
            locks = curr.query(curr.Q.filter(lambda a:a['Path'].startswith('.syncrclone/LOCK')))
            locks = [f'{AB}:{os.path.relpath(l["Path"],".syncrclone/LOCK/")}' for l in locks]
    
        if not error:
            return locks
        
        if locks:
            msg = ['Remote(s) locked:']
            for lock in locks:
                msg.append(f'  {lock}')
            raise LockedRemoteError('\n'.join(msg))
            
    def compare(self,file1,file2):
        """Compare file1 and file2 (may be A or B or curr and prev)"""
        config = self.config
        compare = config.compare # Make a copy as it may get reset (str is immutable so no need to copy)
                
        if not file1:
            return False
        if not file2:
            return False
    
        if compare == 'hash':
            h1 = file1.get('Hashes',{})
            h2 = file2.get('Hashes',{})
    
            # Just because there are common hashes, does *not* mean they are
            # all populated. e.g, it could be a blank string.
            # It is also possible for there to not be common hashes if the lists
            # were not refreshed
            common = {k for k,v in h1.items() if v.strip()}.intersection(\
                      k for k,v in h2.items() if v.strip())
            
            if common:
                return all(h1[k] == h2[k] for k in common)
            
            if not common: 
                msg = 'No common hashes found and/or one or both remotes do not provide hashes'
            else:
                msg = 'One or both remotes are missing hashes'
            
            if config.hash_fail_fallback:
                msg += f". Falling back to '{config.hash_fail_fallback}'"        
                warnings.warn(msg)
                compare = config.hash_fail_fallback
            else:
                raise ValueError(msg)
        
        # Check size either way
        if file1['Size'] != file2['Size']:
            return False
        
        if compare == 'size': # No need to compare mtime
            return True
        
        if 'mtime' not in file1 or 'mtime' not in file2:
            warnings.warn(f"File do not have mtime. Using only size")
            return True # Only got here size is equal
        
        return abs(file1['mtime'] - file2['mtime']) <= config.dt


    def avoid_relist(self):
        # actions: 'new','del','tag','backup','trans','moves'
        # Care?:    N     Y     N     N        Y       Y
        
        # Actions must go first on both sides since we need tags before
        # transfers
        currA = self.currA0.copy()
        currB = self.currB0.copy()
        
        for AB in 'AB':
            if AB == 'A':
                currAB,currBA,BA = currA,currB,'B'
            else:
                currAB,currBA,BA = currB,currA,'A'
           
        
            for filename in getattr(self,f'del{AB}'):
                currAB.remove(Path=filename)
       
            for filenameOLD,filenameNEW in getattr(self,f'moves{AB}'):
                q = currAB.pop({'Path':filenameOLD})
                q['Path'] = filenameNEW
                currAB.add(q)

        for AB in 'AB':    
            if AB == 'A':
                currAB,currBA,BA = currA,currB,'B'
            else:
                currAB,currBA,BA = currB,currA,'A'
            
            for filename in getattr(self,f'trans{BA}2{AB}'):
                if filename.startswith('.syncrclone'): # We don't care about these
                    continue
                    
                q = {'Path':filename}
                if q in currAB: # Remove the old
                    currAB.remove(q)
                file = currBA[q]
                #file['_copied'] = True # Set this so that on the next run, if using reuse_hashes, it is recomputed
                currAB.add(file)

        return currA,currB






