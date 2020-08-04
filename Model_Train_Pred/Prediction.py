#!/usr/bin/env python

############################################################
# import packages needed
import argparse
import time
import subprocess
import io
from io import StringIO
import pandas as pd
import numpy as np
import sys
import multiprocessing

##########################################################
### time calculation
start_time=time.clock()


###########################################################
### variables need
parser = argparse.ArgumentParser(description='manual to this script')

parser.add_argument('--TIGAR_dir',type=str)

### eQTL weight file
parser.add_argument('--weight',type=str,dest='w_path')

### Test sampleID
parser.add_argument('--test_sampleID',type=str,dest='sampleid_path')

### Specified chromosome number
parser.add_argument('--chr',type=str)

### Test genotype files
parser.add_argument('--genofile',type=str,dest='geno_path')

### Specified input file type(vcf or dosages)
parser.add_argument('--genofile_type',type=str)

### 'DS' or 'GT' for VCF genotype file
parser.add_argument('--format',type=str)

### window
parser.add_argument('--window',type=int)

### Gene annotation file
parser.add_argument('--gene_anno',type=str,dest='annot_path')

### number of thread
parser.add_argument('--thread',type=int)

### Threshold of difference of maf between training data and testing data
parser.add_argument('--maf_diff',type=float)

### output dir
parser.add_argument('--out_dir',type=str)

args = parser.parse_args()

sys.path.append(args.TIGAR_dir)
###########################################################
from TIGARutils import calc_maf, call_tabix, call_tabix_header, check_prep_vcf, genofile_cols_dtype, get_header, get_snpIDs, optimize_cols, reformat_sample_vals, reformat_vcf, substr_in_strarray

# return correct snpID and GT value
# 2-GT value if matching snpID is flipped wrt Weight snpID
def handle_flip_pred(df: pd.DataFrame, sampleID, orig_overlap, flip_overlap):
    df = df.copy()
    orig = df['IDorig'].values
    flip = df['IDflip'].values
    origmaf = df['MAF'].values
    df = df[sampleID]

    ids = np.empty_like(orig)
    maf = np.empty_like(origmaf)
    outdf = pd.DataFrame(columns = sampleID)

    for i in range(len(df)):
        if orig[i] in orig_overlap:
            ids[i], maf[i] = orig[i], origmaf[i]
            outdf = outdf.append(df.iloc[i])
        elif flip[i] in flip_overlap:
            ids[i], maf[i] = flip[i], 1-origmaf[i]
            outdf = outdf.append(df.iloc[i].apply(lambda x: 2-x))

    return ids, maf, outdf

#######################################################################
### Input Arguments for GReX Prediction
#######################################################################

# --chr: Chromosome number need to be specified with respect to the genotype input data
# --weight: Path for SNP weight (eQTL effect size) file 
# --test_sampleID: Path for a file with sampleIDs that should be contained in the genotype file
# --genofile: Path for the training genotype file (bgzipped and tabixed) 
# --genofile_type: Genotype file type: "vcf" or "dosage"
# --genofile_colnames : File with column heads of genotype file
# --format: Genotype format in VCF file that should be used: "GT" (default) for genotype data or "DS" for dosage data, only required if the input genotype file is of VCF file
# --window: Window size around gene transcription starting sites (TSS) for selecting cis-SNPs for fitting gene expression prediction model (default 1000000 for +- 1MB region around TSS)
# --maf_diff: MAF difference threshold for matching SNPs from eQTL weight file and test genotype file. If SNP MAF difference is greater than maf_diff (default 0.2), , the SNP will be excluded
# --thread: Number of threads for parallel computation (default 1)
# --out_dir: Output directory (will be created if not exist)

#################################
### check input command
print("Chrmosome: "+args.chr+ "\n")
print("eQTL weight file: "+args.w_path+ "\n")
print("Test gene annotation file: "+args.annot_path+ "\n")
if args.sampleid_path:
    print("Test sampleID file: "+args.sampleid_path+ "\n")

print("Test genotype file: "+args.geno_path+ "\n")

if args.genofile_type=='vcf':
    print("VCF genotype file is used for prediction with genotype format: " + args.format + "\n")
    gcol_sampleids_strt_ind = 9
elif args.genofile_type=='dosage':
    print("Using genotype data from the dosage file for."+ "\n")
    args.format = 'DS'
    gcol_sampleids_strt_ind = 5
else:
    raise SystemExit("Please specify input test genotype file as either 'vcf' or 'dosage'."+ "\n")

print("Gene region size: window = "+str(args.window)+ "\n")

print("MAF difference threshold for matching SNPs from eQTL weight file and test genotype file: "+str(args.maf_diff)+ "\n")

print("Number of threads: "+str(args.thread)+ "\n")
print("Output dir: "+args.out_dir+ "\n")

#########################
# Load eQTL weights (ES)
w_cols = get_header(args.w_path)

w_use_cols = ['CHROM','POS','REF','ALT','TargetID','ES','MAF']
w_dtypes = {'CHROM': object, 'POS': np.int64, 'REF': object, 'ALT': object, 'TargetID': object, 'ES': np.float64, 'MAF': np.float64}

if 'ID' in w_cols:
    w_use_cols.append('ID')
    w_dtypes['ID'] = object

elif 'snpID' in w_cols:
    w_use_cols.append('snpID')
    w_dtypes['snpID'] = object

print("Reading eQTL weights file.")
Weight_chunks=pd.read_csv(
    args.w_path, 
    sep='\t', 
    iterator=True, 
    chunksize=10000,
    usecols=w_use_cols,
    dtype=w_dtypes)
Weight = pd.concat([x[x['CHROM'] == args.chr] for x in Weight_chunks]).reset_index(drop=True)

if Weight.empty:
    raise SystemExit('There are no valid eQTL weights.')

Weight = optimize_cols(Weight)

if 'ID' in Weight.columns:
    Weight.rename(columns={'ID':'snpID'})

if not 'snpID' in Weight.columns:
    Weight['snpID'] = get_snpIDs(Weight)

Weight = Weight.drop_duplicates(['snpID'], keep='first')

# Load annotation file
print("Reading gene annotation file.")
Gene_chunks = pd.read_csv(
    args.annot_path, 
    sep='\t', 
    iterator=True, 
    chunksize=10000,
    dtype={'CHROM':object,'GeneStart':np.int64,'GeneEnd':np.int64,'TargetID':object,'GeneName':object}, 
    usecols=['CHROM','GeneStart','GeneEnd','TargetID','GeneName'])
Gene = pd.concat([x[x['CHROM'] == args.chr] for x in Gene_chunks]).reset_index(drop=True)

if Gene.empty:
    raise SystemExit('There are no valid annotations.')

Gene = optimize_cols(Gene)

# Load genotype column names of test genotype file
g_cols = call_tabix_header(args.geno_path)
gcol_sampleids = g_cols[gcol_sampleids_strt_ind:]

# if user specified path with sampleids, and at least one sampid from that file, get intersection
if args.sampleid_path:
    # Load test sample IDs
    spec_sampleids = pd.read_csv(args.sampleid_path, sep='\t', header=None)
    spec_sampleids = spec_sampleids[0].drop_duplicates()

    sampleID = np.intersect1d(gcol_sampleids, spec_sampleids)

else:
    sampleID = gcol_sampleids

if not sampleID.size:
    raise SystemExit('There are no sampleID in both the genotype data and the specified sampleID file.')

# get the column indices and dtypes for reading genofile into pandas
g_cols_ind, g_dtype = genofile_cols_dtype(g_cols, args.genofile_type, sampleID)

### Define TargetID
TargetID = Weight.TargetID.values
n_targets = TargetID.size

out_cols = np.concatenate((
    ['CHROM','GeneStart','GeneEnd','TargetID','GeneName'], sampleID))

pd.DataFrame(columns=out_cols).to_csv(
    args.out_dir+'/CHR'+str(args.chr) + '_Pred_GReX.txt',
    sep='\t', 
    index=None, 
    header=True, 
    mode='w')

#################################################
### thread function
def thread_process(num):
    try:
        Gene_info = Gene.iloc[[num]]

        start = str(max(int(Gene_info.GeneStart)-args.window, 0))
        end = str(int(Gene_info.GeneEnd)+args.window)

        g_proc_out = call_tabix(args.geno_path, args.chr, start, end)
        
        if not g_proc_out:
            print("Genotype file has no test SNPs in window of gene="+TargetID[num])
            return None  
        
        print("Predict GReX for Gene : "+TargetID[num])
        target_geno = pd.read_csv(StringIO(g_proc_out.decode('utf-8')),
                sep='\t',
                low_memory=False,
                header=None,
                usecols=g_cols_ind,
                dtype=g_dtype)
        target_geno.columns = [g_cols[i] for i in target_geno.columns]
        target_geno = optimize_cols(target_geno)

        # Get original and flipped snpIDs, filter out duplicates
        target_geno['IDorig'] = get_snpIDs(target_geno)
        target_geno['IDflip'] = get_snpIDs(target_geno, flip=True)
        target_geno = target_geno.drop(columns=['CHROM','POS','REF','ALT'])
        target_geno = target_geno.drop_duplicates(['IDorig'], keep='first')

        ### Intersect SNPs from eQTL weight file and test genotype file
        # initial filter to reduce amount of dataframe processing
        target_weight = Weight[Weight.TargetID==TargetID[num]][['snpID', 'ES', 'MAF']]

        if target_weight.empty:
            print("No cis-eQTL weights for gene="+TargetID[num])
            return None       

        # get overlapping snps
        snp_overlap_orig = np.intersect1d(target_weight.snpID, target_geno.IDorig)
        snp_overlap_flip = np.intersect1d(target_weight.snpID, target_geno.IDflip)
        snp_overlap = np.concatenate((snp_overlap_orig, snp_overlap_flip))

        if not snp_overlap.size:
            print("No overlapping test SNPs between weight and genotype file for ="+TargetID[num])
            return None

        target_weight = target_weight[target_weight.snpID.isin(snp_overlap)]
        target_geno = target_geno[target_geno.IDorig.isin(snp_overlap_orig) | target_geno.IDflip.isin(snp_overlap_flip)]

        # vcf files may have data in multiple formats, check if this is the case and remove unnecessary formats. requires that all rows have data in the user-specified format
        if args.genofile_type=='vcf':
            target_geno = check_prep_vcf(target_geno, args.format, sampleID)

        # reformat values in target_geno data frame
        target_geno[sampleID]=target_geno[sampleID].apply(lambda x:reformat_sample_vals(x,args.format), axis=0)

        # calculate MAF
        target_geno = calc_maf(target_geno, sampleID, 0, op=operator.ge)

        # HANDLE FLIPPED SNPS 
        if (snp_overlap_orig.size > 0) and (snp_overlap_flip.size > 0):
            # assume mix of flipped, non-flipped
            target_geno['snpID'], target_geno['MAF_test'], target_geno[sampleID] = handle_flip_pred(target_geno, sampleID,snp_overlap_orig, snp_overlap_flip)

        elif snp_overlap_orig.size == snp_overlap.size:
            # assume all non-flipped 
            target_geno[['snpID','MAF_test']] = target_geno[['IDorig','MAF']]

        else:
            # assume all flipped
            target_geno['snpID'], target_geno['MAF_test'] = target_geno['IDflip'], 1-target_geno['MAF']
            target_geno[sampleID] = target_geno[sampleID].apply(lambda x: 2-x)

        target_geno = target_geno.drop(columns=['IDorig','IDflip','MAF'])

        # merge target_geno, target_weight
        Pred = target_geno.merge(
            target_weight, 
            left_on='snpID', 
            right_on='snpID', 
            how='outer')

        Pred['diff'] = np.abs(Pred['MAF'].values - Pred['MAF_test'].values)
        
        Pred = Pred[Pred['diff']<=args.maf_diff].drop(columns=['MAF','MAF_test','diff'])

        if Pred.empty:
            print("All SNP MAFs for training data and testing data differ by a magnitude greater than "+str(args.maf_diff) + "\n")
            return None

        print("Number of SNPs used for prediction after filtering by maf_diff : "+str(Pred.snpID.size))

        result = Gene_info.copy()
        result[sampleID] = pd.DataFrame(np.dot(Pred[sampleID].T, Pred['ES'].values)).T

        # output results
        result.to_csv(
            args.out_dir+'/CHR'+str(args.chr)+'_Pred_GReX.txt',
            sep='\t',
            index=None,
            header=None,
            mode='a')

    except Exception as e:
        e_type, e_obj, e_tracebk = sys.exc_info()
        e_line_num = e_tracebk.tb_lineno

        e, e_type, e_line_num = [str(x) for x in [e, e_type, e_line_num]]

        print('Caught a type '+ e_type +' exception for TargetID='+TargetID[num]+', num=' + str(num) + ' on line '+e_line_num+':\n' + e )

    finally:
        # print info to log do not wait for buffer to fill up
        sys.stdout.flush()       

#################################################
# thread begin
if __name__ == '__main__':
    print("Starting prediction for "+str(n_targets)+" target genes.")
    pool = multiprocessing.Pool(args.thread)
    pool.map(thread_process,[num for num in range(n_targets)])
    pool.close()
    pool.join()

#################################################
### time calculation
time=round((time.clock()-start_time)/60,2)

print(str(time)+' minutes')


