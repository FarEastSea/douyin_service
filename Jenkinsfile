pipeline {
    agent any

    stages {
        stage('Deploy') {
            steps {
                sshagent(credentials: ['6c777370-40da-4900-bdef-c145d59e7b3b']) {
                    sh '''
                        ssh -o StrictHostKeyChecking=no -p 43004 root@192.168.31.155 "
                            cd /www/wwwroot/douyin_service &&
                            chmod +x deploy.sh &&
                            ./deploy.sh
                        "
                    '''
                }
            }
        }
    }

    post {
        success {
            echo 'Deploy success.'
        }
        failure {
            echo 'Deploy failed.'
        }
    }
}