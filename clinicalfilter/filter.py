'''
Copyright (c) 2016 Genome Research Ltd.

Permission is hereby granted, free of charge, to any person obtaining a copy of
this software and associated documentation files (the "Software"), to deal in
the Software without restriction, including without limitation the rights to
use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies
of the Software, and to permit persons to whom the Software is furnished to do
so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS
FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR
COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER
IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN
CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
'''

import json
import sys
import logging

from clinicalfilter.load_vcfs import LoadVCFs
from clinicalfilter.inheritance import Allosomal, Autosomal, CNVInheritance
from clinicalfilter.post_inheritance_filter import PostInheritanceFilter
from clinicalfilter.reporting import Report
from clinicalfilter.utils import get_vcf_provenance
from clinicalfilter.load_files import open_known_genes, \
    create_person_ID_mapper, open_cnv_regions, open_last_base_sites

class Filter(object):
    """ filters trios for candidate variants that might contribute to a
    probands disorder.
    """
    
    def __init__(self, population_tags=None, count=0, known_genes=None, genes_date=None,
            alternate_ids=None, regions=None, lof_sites=None, pp_filter=0.0,
            output_path=None, export_vcf=None, debug_chrom=None, debug_pos=None):
        """ initialise the class object
        
        Args:
            count: number of probands to analyse, helpful for tracking progress
                in output logs.
            known_genes: path to table of genes genes known to be associated
                with genetic disorders, or None.
            genes_date: date of the known_genes file, or None if not using/unknown.
            alternate_ids: path to table linking proband IDs to an alternate ID scheme.
            regions: path to a table of regions for DECIPHER CNV syndromes.
            lof_sites: path to json file of [chrom, position] coordinates in
                genome, for modifying to a loss-of-function consequence if
                required. Can be None if unneeded.
            pp_filter: threshold from 0 to 1 for pp_dnm value to filter out
                candidiate DNMs which fall below this value
            output_path: path to write output tab-separated file to
            export_vcf: path to file or folder to write VCFs to.
            debug_chrom: chromosome for debugging purposes.
            debug_pos: position for debugging variant filtering at.
        """
        
        self.pp_filter = pp_filter
        
        self.debug_chrom = debug_chrom
        self.debug_pos = debug_pos
        
        # open reference datasets, these return None if the paths are None
        self.known_genes = open_known_genes(known_genes)
        self.id_mapper = create_person_ID_mapper(alternate_ids)
        self.cnv_regions = open_cnv_regions(regions)
        self.last_base = open_last_base_sites(lof_sites)
        
        self.loader = LoadVCFs(count, population_tags, self.known_genes, self.last_base,
             self.debug_chrom, self.debug_pos)
        
        self.reporter = Report(output_path, export_vcf, self.id_mapper, genes_date)
    
    def filter_trio(self, family):
        """ loads trio variants, and screens for candidate variants
        """
        
        # some families have more than one child in the family, so run
        # through each child.
        family.set_child()
        while family.child is not None:
            if family.child.is_affected():
                found_vars = self.analyse_trio(family)
                
                vcf_provenance = [ get_vcf_provenance(x) for x in
                    [family.child, family.mother, family.father] ]
                
                # export the results to either tab-separated table or VCF format
                self.reporter.export_data(found_vars, family, \
                    self.loader.child_header, vcf_provenance)
            
            family.set_child_examined()
    
    def analyse_trio(self, family):
        """identify candidate variants in exome data for a single trio.
        
        takes variants that passed the initial filtering from VCF loading, and
        splits the variants into groups for each gene with variants. Then
        analyses variants in a single gene (so we can utilise the appropriate
        inheritance mechanisms for that gene), before running some
        pos-inheritance filters, and exporting the data (ir required).
        
        Args:
            variants: list of TrioGenotypes objects
        """
        
        variants = self.loader.get_trio_variants(family, self.pp_filter)
        
        # organise variants by gene, then find variants that fit
        # different inheritance models
        genes_dict = self.create_gene_dict(variants)
        found_vars = []
        for gene in genes_dict:
            gene_vars = genes_dict[gene]
            found_vars += self.find_variants(gene_vars, gene, family)
        
        # remove any duplicate variants (which might ocur due to CNVs being
        # checked against all the genes that they encompass)
        found_vars = self.exclude_duplicates(found_vars)
        
        # apply some final filters to the flagged variants
        post_filter = PostInheritanceFilter(found_vars, family, self.debug_chrom,
            self.debug_pos)
        
        return post_filter.filter_variants()
    
    def create_gene_dict(self, variants):
        """creates dictionary of variants indexed by gene
        
        Args:
            variants: list of TrioGenotypes objects
        
        Returns:
            dictionary of variants indexed by HGNC symbols
        """
        
        # organise the variants into entries for each gene
        genes = {}
        for var in variants:
            # variants (particularly CNVs) can span multiple genes, so we need
            # to check each gene separately, and then collapse duplicates later
            for gene in var.get_genes():
                if gene not in genes:
                    genes[gene] = []
                # add the variant to the gene entry
                genes[gene].append(var)
        
        return genes
        
    def find_variants(self, variants, gene, family):
        """ finds variants that fit inheritance models
        
        Args:
            variants: list of TrioGenotype objects
            gene: gene ID as string
        
        Returns:
            list of variants that pass inheritance checks
        """
        
        # get the inheritance for the gene (monoalleleic, biallelic, hemizygous
        # etc), but allow for times when we haven't specified a list of genes
        # to use
        gene_inh = None
        if self.known_genes is not None and gene in self.known_genes:
            gene_inh = self.known_genes[gene]["inh"]
        
        # If we are looking for variants in a set of known genes, and the gene
        # isn't part of that set, then we don't ant to examine the variant for
        # that gene, UNLESS the variant is a CNV, since CNVs can be included
        # purely from size thresholds, regardless of which gene they overlap.
        if self.known_genes is not None and gene not in self.known_genes:
            variants = [ x for x in variants if x.is_cnv() ]
        
        # ignore intergenic variants
        if gene is None:
            for var in variants:
                if var.get_chrom() == self.debug_chrom and var.get_position() == self.debug_pos:
                    print(var, "lacks HGNC/gene symbol")
            return []
        
        # Now that we are examining a single gene, check that the consequences
        # for the gene are in the required functional categories.
        variants = [ var for var in variants if var.child.is_lof(gene) or var.child.is_missense(gene) ]
        if variants == []:
            return []
        
        logging.info("{}\t{}\tvariants: {}\trequired_mode: {}".format(family.child.get_id(), gene,
            [str(x) for x in variants], gene_inh))
        chrom_inheritance = variants[0].get_inheritance_type()
        
        if chrom_inheritance == "autosomal":
            finder = Autosomal(variants, family, self.known_genes, gene, self.cnv_regions)
        elif chrom_inheritance in ["XChrMale", "XChrFemale", "YChrMale"]:
            finder = Allosomal(variants, family, self.known_genes, gene, self.cnv_regions)
        
        variants = finder.get_candidate_variants()
        variants = [ (x[0], list(x[1]), list(x[2]), [gene]) for x in variants ]
        
        return variants
    
    def exclude_duplicates(self, variants):
        """ rejig variants included under multiple inheritance mechanisms
        
        Args:
            variants: list of candidate variants
        
        Returns:
            list of (variant, check_type, inheritance) tuples, with duplicates
            excluded, and originals modified to show both mechanisms
        """
        
        unique_vars = {}
        for variant in variants:
            key = variant[0].child.get_key()
            if key not in unique_vars:
                unique_vars[key] = list(variant)
            else:
                result = variant[1]
                inh = variant[2]
                hgnc = variant[3]
                
                # append the check type and inheritance type to the first
                # instance of the variant
                unique_vars[key][1] += [x for x in result if x not in unique_vars[key][1]]
                unique_vars[key][2] += [x for x in inh if x not in unique_vars[key][2]]
                
                # add the HGNC symbols that are unique to the current variant
                # to the merged variant
                hgnc = [x for x in hgnc if x not in unique_vars[key][3]]
                unique_vars[key][3] += hgnc
                
        unique_vars = [tuple(unique_vars[x]) for x in unique_vars]
        
        return unique_vars
