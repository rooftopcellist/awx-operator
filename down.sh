#!/bin/bash
# AWX Operator down.sh
# Purpose:
#   Cleanup and delete the namespace you deployed in

# -- Usage
#   NAMESPACE=awx ./down.sh

# -- Variables
TAG=${TAG:-dev}
AWX_CR=${AWX_CR:-awx}
CLEAN_DB=${CLEAN_DB:-false}


# -- Check for required variables
# Set the following environment variables
# export NAMESPACE=awx

if [ -z "$NAMESPACE" ]; then
  echo "Error: NAMESPACE env variable is not set. Run the following with your namespace:"
  echo "  export NAMESPACE=developer"
  exit 1
fi

# -- Delete Backups
kubectl delete awxbackup --all

# -- Delete Restores
kubectl delete awxrestore --all

# Deploy Operator
make undeploy NAMESPACE=$NAMESPACE

# Remove PVCs
kubectl delete pvc postgres-15-$AWX_CR-postgres-15-0

