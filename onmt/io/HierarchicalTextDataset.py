# -*- coding: utf-8 -*-

from collections import Counter
from itertools import chain
import io, os, pdb
import codecs
import sys

import torch
import torchtext

from onmt.Utils import aeq
from onmt.io.DatasetBase import (ONMTDatasetBase, UNK_WORD,
                                 PAD_WORD, BOS_WORD, EOS_WORD)

TRUNC_OUTER = True
TRUNC_OUTER_START = -5
TRUNC_OUTER_END = False

class HierarchicalTextDataset(ONMTDatasetBase):
    """ Dataset for data_type=='hierarchicalText'

        Build `Example` objects, `Field` objects, and filter_pred function
        from text corpus.

        Args:
            fields (dict): a dictionary of `torchtext.data.Field`.
                Keys are like 'src', 'tgt', 'src_map', and 'alignment'.
            src_examples_iter (dict iter): preprocessed source example
                dictionary iterator.
            tgt_examples_iter (dict iter): preprocessed target example
                dictionary iterator.
            num_src_feats (int): number of source side features.
            num_tgt_feats (int): number of target side features.
            src_seq_length (int): maximum source sequence length.
            tgt_seq_length (int): maximum target sequence length.
            dynamic_dict (bool): create dynamic dictionaries?
            use_filter_pred (bool): use a custom filter predicate to filter
                out examples?
    """
    def __init__(self, fields, src_examples_iter, tgt_examples_iter,
                 num_src_feats=0, num_tgt_feats=0,
                 src_seq_length=0, tgt_seq_length=0,
                 dynamic_dict=True, use_filter_pred=True):
        self.data_type = 'hierarchicalText'

        if src_seq_length!=0:
            self.src_len = src_seq_length

        # self.src_vocabs: mutated in dynamic_dict, used in
        # collapse_copy_scores and in Translator.py
        self.src_vocabs = []

        self.n_src_feats = num_src_feats
        self.n_tgt_feats = num_tgt_feats

        # Each element of an example is a dictionary whose keys represents
        # at minimum the src tokens and their indices and potentially also
        # the src and tgt features and alignment information.
        if tgt_examples_iter is not None:
            examples_iter = (self._join_dicts(src, tgt) for src, tgt in
                             zip(src_examples_iter, tgt_examples_iter))
        else:
            examples_iter = src_examples_iter

        if dynamic_dict:
            examples_iter = self._dynamic_dict(examples_iter)

        # Peek at the first to see which fields are used.
        ex, examples_iter = self._peek(examples_iter)
        keys = ex.keys() # TODO: What is the format of keys here?
        #pdb.set_trace()
        out_fields = [(k, fields[k]) if k in fields else (k, None)
                      for k in keys]
        example_values = ([ex[k] for k in keys] for ex in examples_iter) 

        # If out_examples is a generator, we need to save the filter_pred
        # function in serialization too, which would cause a problem when
        # `torch.save()`. Thus we materialize it as a list.
        src_size = 0

        out_examples = []
        for ex_values in example_values:
            example = self._construct_example_fromlist(
                ex_values, out_fields)
            src_size += len(example.src) #TODO: This is probably n_sents, is this okay?
            out_examples.append(example)

        print("average src size", src_size / len(out_examples),
              len(out_examples))

        def filter_pred(example):
            return 0 < len(example.src) <= src_seq_length \
               and 0 < len(example.tgt) <= tgt_seq_length

        filter_pred = filter_pred if use_filter_pred else lambda x: True
        
        super(HierarchicalTextDataset, self).__init__(
            out_examples, out_fields,
            lambda(x):True#filter_pred TODO: Why doesn't this work with the field's fix_length?
        )

    def sort_key(self, ex):
        """ Sort using length of source sentences. """
        # Default to a balanced sort, prioritizing tgt len match.
        # TODO: make this configurable.
        if hasattr(ex, "tgt"):
            return len(ex.src), len(ex.tgt) #TODO: This is probably n_sents, is this okay?
        return len(ex.src)

    @staticmethod
    def collapse_copy_scores(scores, batch, tgt_vocab, src_vocabs):
        """
        Given scores from an expanded dictionary
        corresponeding to a batch, sums together copies,
        with a dictionary word when it is ambigious.

        TODO: How does this get changed for hierarchical data?
        """
        offset = len(tgt_vocab)
        for b in range(batch.batch_size):
            blank = []
            fill = []
            index = batch.indices.data[b]
            src_vocab = src_vocabs[index]
            for i in range(1, len(src_vocab)):
                sw = src_vocab.itos[i]
                ti = tgt_vocab.stoi[sw]
                if ti != 0:
                    blank.append(offset + i)
                    fill.append(ti)
            if blank:
                blank = torch.Tensor(blank).type_as(batch.indices.data)
                fill = torch.Tensor(fill).type_as(batch.indices.data)
                scores[:, b].index_add_(1, fill,
                                        scores[:, b].index_select(1, blank))
                scores[:, b].index_fill_(1, blank, 1e-10)
        return scores

    @staticmethod
    def make_text_examples_nfeats_tpl(path, truncate, side):
        """
        Args:
            path (str): location of a dir with src or tgt files
            truncate (int): maximum sequence length (0 for unlimited).
            side (str): "src" or "tgt".

        Returns:
            (example_dict iterator, num_feats) tuple.
        """
        assert side in ['src', 'tgt']

        if path is None:
            return (None, 0)

        # All examples have same number of features, so we peek first one
        # to get the num_feats.
        examples_nfeats_iter = \
            HierarchicalTextDataset.read_text_dir(path, truncate, side)

        first_ex = next(examples_nfeats_iter)
        num_feats = first_ex[1]

        # Chain back the first element - we only want to peek it.
        examples_nfeats_iter = chain([first_ex], examples_nfeats_iter)
        examples_iter = (ex for ex, nfeats in examples_nfeats_iter)

        return (examples_iter, num_feats)

    @staticmethod
    def read_text_dir(path, truncate, side):
        """
        Args:
            path (str): location of a directory containing src or tgt files.
            truncate (int): maximum sequence length (0 for unlimited).
            side (str): "src" or "tgt".

        Yields:
            (word, features, nfeat) triples for each line.

        """
        filelist = [os.path.join(path,el) for el in os.listdir(path)]
        for i,path in enumerate(filelist):
            with codecs.open(path, "r", "utf-8") as corpus_file:
                if side=="tgt":
                    text = " ".join(corpus_file.read().split("\n"))
                    line = line.strip().split()
                    if truncate:
                        line = line[:truncate]

                    words, feats, n_feats = \
                                HierarchicalTextDataset.extract_text_features(line)

                    example_dict = {side: words, "indices": i}
                    if feats:
                        prefix = side + "_feat_"
                        example_dict.update((prefix + str(j), f)
                                             for j, f in enumerate(feats))
                else:
                    example_dict = {side: [], "indices": i} #TODO: Do we need sentence indicies?
                    for j, line in enumerate(corpus_file):
                        line = line.strip().split()

                        if truncate: #TODO: way to restrict number of lines in post?
                            line = line[:truncate]

                        words, feats, n_feats = \
                                HierarchicalTextDataset.extract_text_features(line)

                        example_dict[side].append(words)
                        
                        #TODO: this is not going to work
                        if feats:
                            prefix = side + "_feat_"
                            example_dict.update((prefix + str(k), f)
                                                for k, f in enumerate(feats))
                    if TRUNC_OUTER_START:
                        example_dict[side] = example_dict[side][TRUNC_OUTER_START:]
                    if TRUNC_OUTER_END:
                        example_dict[side] = example_dict[side][:TRUNC_OUTER_END]
            yield example_dict, n_feats


            
    @staticmethod
    def get_fields(n_src_features, n_tgt_features):
        """
        Args:
            n_src_features (int): the number of source features to
                create `torchtext.data.Field` for.
            n_tgt_features (int): the number of target features to
                create `torchtext.data.Field` for.

        Returns:
            A dictionary whose keys are strings and whose values
            are the corresponding Field objects.
        """
        fields = {}

        #TODO: do we need BOS, EOS tokens here?
        #TODO: What will happen now that lengths are not included??
        src_inner = torchtext.data.Field(init_token=BOS_WORD, eos_token=EOS_WORD,
                                         pad_token=PAD_WORD, include_lengths=True)

        fields["src"] = torchtext.data.NestedField(src_inner, include_lengths=True)

        #TODO: I don't think these will work
        for j in range(n_src_features):
            fields["src_feat_"+str(j)] = \
                torchtext.data.Field(pad_token=PAD_WORD)

        fields["tgt"] = torchtext.data.Field(
            init_token=BOS_WORD, eos_token=EOS_WORD,
            pad_token=PAD_WORD)

        for j in range(n_tgt_features):
            fields["tgt_feat_"+str(j)] = \
                torchtext.data.Field(init_token=BOS_WORD, eos_token=EOS_WORD,
                                     pad_token=PAD_WORD)

        def make_src(data, vocab, is_train):
            src_size = max([t.size(0) for t in data])
            src_vocab_size = max([t.max() for t in data]) + 1
            alignment = torch.zeros(src_size, len(data), src_vocab_size)
            for i, sent in enumerate(data):
                for j, t in enumerate(sent):
                    alignment[j, i, t] = 1
            return alignment

        #TODO: Make CopyGenerator work with something like this:
        map_inner = torchtext.data.Field(
            use_vocab=False, tensor_type=torch.FloatTensor,
            postprocessing=make_src, sequential=False)
        
        fields["src_map"] = torchtext.data.NestedField(map_inner)
        
        def make_tgt(data, vocab, is_train):
            tgt_size = max([t.size(0) for t in data])
            alignment = torch.zeros(tgt_size, len(data)).long()
            for i, sent in enumerate(data):
                alignment[:sent.size(0), i] = sent
            return alignment

        fields["alignment"] = torchtext.data.Field(
            use_vocab=False, tensor_type=torch.LongTensor,
            postprocessing=make_tgt, sequential=False)

        fields["indices"] = torchtext.data.Field(
            use_vocab=False, tensor_type=torch.LongTensor,
            sequential=False)

        return fields

    @staticmethod
    def get_num_features(corpus_file, side):
        """
        Peek one line and get number of features of it.
        (All lines must have same number of features).
        For text corpus, both sides are in text form, thus
        it works the same.

        Args:
            corpus_file (str): file path to get the features.
            side (str): 'src' or 'tgt'.

        Returns:
            number of features on `side`.
        """
        corpus_file = os.path.join(corpus_file,os.listdir(corpus_file)[0])
        with codecs.open(corpus_file, "r", "utf-8") as cf:
            f_line = cf.readline().strip().split()
            _, _, num_feats = HierarchicalTextDataset.extract_text_features(f_line)

        return num_feats

    # Below are helper functions for intra-class use only.
    def _dynamic_dict(self, examples_iter):
        for example in examples_iter:
            src = example["src"]
            src_vocab = torchtext.vocab.Vocab(Counter([w for seq in src for w in seq]),
                                              specials=[UNK_WORD, PAD_WORD])
            self.src_vocabs.append(src_vocab)
            # Mapping source tokens to indices in the dynamic dict.
            src_map = torch.LongTensor([[src_vocab.stoi[w] for w in seq] for seq in src])
            example["src_map"] = src_map

            if "tgt" in example:
                tgt = example["tgt"]
                mask = torch.LongTensor(
                    [0] + [src_vocab.stoi[w] for w in tgt] + [0])
                example["alignment"] = mask
            yield example


class ShardedHierarchicalTextCorpusIterator(object):
    """
    This is the iterator for text corpus, used for sharding large text
    corpus into small shards, to avoid hogging memory.

    Inside this iterator, it automatically divides the corpus file into
    shards of size `shard_size`. Then, for each shard, it processes
    into (example_dict, n_features) tuples when iterates.
    """
    def __init__(self, corpus_path, line_truncate, side, shard_size,
                 assoc_iter=None):
        """
        Args:
            corpus_path: the corpus file path.
            line_truncate: the maximum length of a line to read.
                            0 for unlimited.
            side: "src" or "tgt".
            shard_size: the shard size, 0 means not sharding the file.
            assoc_iter: if not None, it is the associate iterator that
                        this iterator should align its step with.
        """
        print "ShardedHierarchicalTextCorpusIterator {} {}".format(side, line_truncate, corpus_path)
        try:
            # The codecs module seems to have bugs with seek()/tell(),
            # so we use io.open().
            self.filelist = (os.path.join(corpus_path,el) for el in os.listdir(corpus_path))
        except IOError:
            sys.stderr.write("Failed to open corpus file: %s" % corpus_path)
            sys.exit(1)

        self.line_truncate = line_truncate
        self.post_truncate = line_truncate #TODO: really would like to decouple these
        self.side = side
        self.shard_size = shard_size
        self.cur_pos = 0
        self.assoc_iter = assoc_iter
        self.last_pos = 0
        self.file_index = -1
        self.eof = False

    def __iter__(self):
        """
        Iterator of (example_dict, nfeats).
        On each call, it iterates over as many (example_dict, nfeats) tuples
        until this shard's size equals to or approximates `self.shard_size`.
        """
        iteration_index = -1
        if self.assoc_iter is not None:
            # We have associate iterator, just yields tuples
            # util we run parallel with it.
            while self.file_index < self.assoc_iter.file_index:
                #TODO: build this
                try:
                    path = self.filelist.next()
                except:
                    raise AssertionError(
                        "Two corpuses must have same number of lines!")
                    
                self.file_index += 1
                iteration_index += 1
                yield self._example_dict_iter(path, iteration_index)

            if self.assoc_iter.eof:
                self.eof = True
                self.corpus.close()
        else:
            # Yield tuples util this shard's size reaches the threshold.
            #self.corpus.seek(self.last_pos)
            while True:
                try:
                    path = self.filelist.next()
                except:
                    self.eof = True
                    raise StopIteration
                #have to bite the bullet on a big source file at some point
                if self.shard_size != 0 and self.cur_pos>=0:
                    next_size = os.path.getsize(path)
                    if next_size+self.cur_pos>=self.shard_size:
                        self.cur_pos = 0
                        self.filelist = chain([path], self.filelist)
                        raise StopIteration
                #add next file to shard
                self.cur_pos+=next_size
                self.file_index += 1
                iteration_index += 1
                yield self._example_dict_iter(path, iteration_index)

    def hit_end(self):
        return self.eof

    @property
    def num_feats(self):
        # We peek the first line of the first file
        path = self.filelist.next()

        with codecs.open(path, "r", "utf-8") as corpus_file:
            line = corpus_file.readline().split()
            if self.line_truncate:
                line = line[:self.line_truncate]
            _, _, self.n_feats = HierarchicalTextDataset.extract_text_features(line)

        # chain the peeked file back on
        self.filelist = chain([path], self.filelist)

        return self.n_feats

    def _example_dict_iter(self, path, index):
        #Get all the feats for the self.side part of an example
        #from the file at path
        with codecs.open(path, "r", "utf-8") as corpus_file:

            if self.side=="tgt":
                text = " ".join(corpus_file.read().split("\n"))
                line = text.strip().split()
                if self.line_truncate:
                    line = line[:self.line_truncate]

                words, feats, n_feats = \
                        HierarchicalTextDataset.extract_text_features(line)
            
                example_dict = {self.side: words, "indices": index}
                if feats:
                    aeq(self.n_feats, n_feats)
                            
                    prefix = self.side + "_feat_"
                    example_dict.update((prefix + str(j), f)
                                        for j, f in enumerate(feats))
            else:
                example_dict = {self.side: [], "indices": index} #TODO: Do we need sentence indicies?
                for j, line in ((e for e in enumerate(corpus_file) if e[0]<self.post_truncate)
                                if self.post_truncate else enumerate(corpus_file)):
                    
                    line = line.strip().split()

                    if self.line_truncate:
                        line = line[:self.line_truncate]

                    words, feats, n_feats = \
                            HierarchicalTextDataset.extract_text_features(line)
            
                    example_dict[self.side].append(words)
                    #TODO: this is not going to work
                    if feats:
                        prefix = self.side + "_feat_"
                        example_dict.update((prefix + str(k), f)
                                            for k, f in enumerate(feats))
                if TRUNC_OUTER_START:
                    example_dict[self.side] = \
                                    example_dict[self.side][TRUNC_OUTER_START:]
                if TRUNC_OUTER_END:
                    example_dict[self.side] = \
                                    example_dict[self.side][:TRUNC_OUTER_END]
        return example_dict

