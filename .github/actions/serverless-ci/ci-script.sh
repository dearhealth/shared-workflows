poetry install
npm install
npm install -g serverless
    
mkdir dh-packages
sls package --stage dev --region us-east-1 --package dh-packages/dev-us-build
          
sls package --stage tst --region us-east-1 --package dh-packages/tst-us-build
          
sls package --stage acc --region us-east-1 --package dh-packages/acc-us-build
sls package --stage prd --region us-east-1 --package dh-packages/prd-us-build 
sls package --stage acc --region eu-west-1 --package dh-packages/acc-eu-build
sls package --stage prd --region eu-west-1 --package dh-packages/prd-eu-build

tar cf packages.tar dh-packages
